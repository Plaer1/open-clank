import { mediaResultButton, resourceResultButton, createCapturedHtmlPreview, capturedResourceButton } from './chatRenderer.js';
import { mountPlanFeedback } from './planFeedback.js';
import { captureDeliverableAppearance } from './theme.js';
import { renderSkillUseReceipt, currentSkillSourceButton } from './skillUseReceipt.js';
import { searchHitButtons } from './toolResultReceipt.js';
import { capturedChangeButton, capturedTurnButton } from './historyView.js';
import { mountChatContextInspector } from "./chatContextInspector.js";
import { uiIcon } from './uiIcons.js';
import { restoreValidatedComposerDraft } from './composerDraftRestore.js';
import { appendCodeEvidenceCAS } from './codeEvidenceComposer.js';
import { stageGeneratedSummaryCAS } from './generatedSummaryComposer.js';
import { beginAttachmentEdit } from './editor/attachmentEditBridge.js';
import { openPendingAttachment, observeCodeActivity, captureCodeActivityPreference } from './copal.js';
import { publishCodeContextBoundary } from './copal/codeActivityModel.js';
import { captureContinuityScope, publishLiveAttention } from './chatContinuityPanel.js';
// static/js/chat.js

/**
 * Main chat functionality - message handling and streaming
 */
// ES6 module — IIFE removed

import Storage from './storage.js';
import uiModule from './ui.js';
import sessionModule from './sessions.js';
import chatRenderer from './chatRenderer.js';
import chatStream from './chatStream.js';
import { addAITTSButton } from './tts-ai.js';
import markdownModule from './markdown.js';
import spinnerModule from './spinner.js';
import presetsModule from './presets.js';
import fileHandlerModule from './fileHandler.js';
import searchModule from './search.js';
import documentModule from './document.js?v=20260722emailfastindex1';
import * as emailInbox from './emailInbox.js?v=20260722emailfastindex1';
import codeRunnerModule from './codeRunner.js';
import slashCommands, { initSlashCommands, isCommand, handleSlashCommand, handleSetupInput, handleSetupWizard, typewriterInto, forkConversation } from './slashCommands.js';
import createResearchSynapse from './researchSynapse.js';
import { createStreamRenderer } from './streamingRenderer.js';
import { wireArrowUpRecall, getUserMessagesFromChatHistory } from './composerArrowUpRecall.js?v=20260714promptrecall';
import {
  applyModelMetricsState,
  applyModelRouteEventState,
  inheritModelRouteState,
} from './chatModelProvenance.js';
import { createTerminalStreamError, isRecoverableStreamError } from './chatStreamErrors.js';
import { createLiveThinkingThrottle } from './liveThinkingThrottle.js';
import * as inputDelivery from './chatInputDelivery.js';
import { restoreReasoningEffortSelection, serializeReasoningEffortSelection } from './modelRequestOptions.js';
import {
  claimAssistantContext,
  commitAssistantContext,
  restoreAssistantContext,
} from './contextualHelp.js';

  const RESEARCH_TIMEOUT_MS = 360000;
  const DEFAULT_TIMEOUT_MS = 120000;
  const RESEARCH_SVG = uiIcon("search", 16);

  let API_BASE = '';
  let currentAbort = null;
  let isStreaming = false;
  // Continuous stall watchdog: while streaming, if the SSE stream produces
  // NOTHING for STALL_THRESHOLD_MS (no deltas, no tool heartbeat — tools beat
  // every 2s, so a full minute of silence means it's genuinely stuck or the
  // model quietly stopped), surface a non-destructive "still working?" prompt
  // instead of silently hanging. Replaces relying only on the tab-refocus
  // recovery (which fired only on visibilitychange and silently reloaded).
  let _stallWatchdog = null;
  let _stallBannerShown = false;
  const STALL_THRESHOLD_MS = 60000;
  let _sendInFlight = false;   // covers the window from click → streaming start
  let _displayOverride = null; // Override visible user bubble text (hides injected prompts)
  let _hideUserBubble = false; // Skip user bubble entirely (e.g. continue after stop)
  let _contextHeaderSeq = 0;
  let _contextHeaderData = null;
  let _contextHeaderBound = false;

  function _fmtContextNumber(n) {
    const v = Number(n || 0);
    return v ? v.toLocaleString() : '?';
  }

  function _contextColorClass(pct) {
    const n = Number(pct || 0);
    if (n >= 85) return 'danger';
    if (n >= 70) return 'warn';
    return '';
  }

  function _contextRingColor(pct) {
    const n = Number(pct || 0);
    if (n >= 85) return 'var(--red, #e06c75)';
    if (n >= 70) return '#ff9900';
    return 'var(--green, #98c379)';
  }

  function _contextRingMarkup(pct, { includeLabel = true, labelId = '' } = {}) {
    const value = Math.max(0, Math.min(100, Number(pct || 0)));
    const r = 6;
    const stroke = 1.5;
    const circ = 2 * Math.PI * r;
    const fill = circ * (value / 100);
    const label = value.toFixed(value >= 10 ? 0 : 1);
    const idAttr = labelId ? ` id="${labelId}"` : '';
    return `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <circle cx="7" cy="7" r="${r}" fill="none" stroke="var(--border, #333)" stroke-width="${stroke}" opacity="0.3"/>
        <circle cx="7" cy="7" r="${r}" fill="none" stroke="var(--ctx-stroke)" stroke-width="${stroke}"
          stroke-dasharray="${fill} ${circ - fill}" stroke-dashoffset="${circ * 0.25}"
          stroke-linecap="round" transform="rotate(-90 7 7)"/>
      </svg>${includeLabel ? `<span class="ctx-ring-pct"${idAttr}>${label}%</span>` : ''}`;
  }

  function _renderContextHeaderRing(pill, pct) {
    const value = Math.max(0, Math.min(100, Number(pct || 0)));
    pill.style.setProperty('--ctx-color', _contextRingColor(value));
    pill.innerHTML = _contextRingMarkup(value, { includeLabel: true, labelId: 'chat-context-pill-label' });
  }

  function _renderCompactMenuContextIcon(pct) {
    const icon = document.querySelector('#export-compact-btn .dropdown-icon');
    if (!icon) return;
    const value = Math.max(0, Math.min(100, Number(pct || 0)));
    const row = document.getElementById('export-compact-btn');
    const color = _contextRingColor(value);
    if (row) row.style.setProperty('--ctx-color', color);
    icon.style.setProperty('--ctx-color', color);
    icon.innerHTML = _contextRingMarkup(value, { includeLabel: false });
  }

  function _liveSessionModule() {
    return (window.sessionModule && window.sessionModule.getCurrentSessionId)
      ? window.sessionModule
      : sessionModule;
  }

  let _contextInspector = null;
  function _closeContextHeaderPopup() {
    _contextInspector?.dispose();
    _contextInspector = null;
    document.querySelectorAll('.chat-context-popup').forEach(el => el.remove());
    const pill = document.getElementById('chat-context-pill');
    if (pill) pill.classList.remove('open');
  }

  function _positionContextHeaderPopup(popup, pill) {
    const rect = pill.getBoundingClientRect();
    popup.style.top = `${Math.round(rect.bottom + 8)}px`;
    popup.style.left = `${Math.round(rect.left + (rect.width / 2) - 119)}px`;
    document.body.appendChild(popup);
    const pRect = popup.getBoundingClientRect();
    if (pRect.left < 8) popup.style.left = '8px';
    if (pRect.right > window.innerWidth - 8) popup.style.left = `${Math.max(8, window.innerWidth - pRect.width - 8)}px`;
    if (pRect.bottom > window.innerHeight - 8) popup.style.top = `${Math.max(8, rect.top - pRect.height - 8)}px`;
  }

  function _showContextHeaderPopup() {
    const pill = document.getElementById('chat-context-pill');
    if (!pill || pill.hidden || !_contextHeaderData) return;
    const wasOpen = pill.classList.contains('open');
    _closeContextHeaderPopup();
    if (wasOpen) return;

    const d = _contextHeaderData;
    const pct = Number(d.context_percent || 0);
    const colorClass = _contextColorClass(pct);
    const modelShort = String(d.model || 'Unknown').split('/').pop();
    const popup = document.createElement('div');
    popup.className = `chat-context-popup ${colorClass}`.trim();

    const title = document.createElement('div');
    title.className = 'chat-context-popup-title';
    title.textContent = 'Chat Context · whole-history estimate';
    popup.appendChild(title);

    const bar = document.createElement('div');
    bar.className = 'chat-context-popup-bar';
    const fill = document.createElement('div');
    fill.className = 'chat-context-popup-fill';
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    bar.appendChild(fill);
    popup.appendChild(bar);

    const rows = [
      ['Whole projection (estimated)', `${_fmtContextNumber(d.used_tokens)} / ${_fmtContextNumber(d.context_length)}`],
      ['Last request (reported)', d.last_request ? `${_fmtContextNumber(d.last_request.tokens)} tokens · SDK · ${d.last_request.as_of}` : 'Unavailable'],
      ['Unsent tail', 'Unavailable'],
      ['Usage', `${pct}%`],
      ['Window model', modelShort],
      ['Messages', `${Number(d.messages || 0).toLocaleString()}`],
    ];
    rows.forEach(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'chat-context-popup-row';
      const a = document.createElement('span');
      a.textContent = label;
      const b = document.createElement('span');
      b.textContent = value;
      row.appendChild(a);
      row.appendChild(b);
      popup.appendChild(row);
    });

    _contextInspector = mountChatContextInspector(popup, () => _liveSessionModule()?.getCurrentSessionId?.() || '');

    if (d.can_compact) {
      const compactBtn = document.createElement('button');
      compactBtn.type = 'button';
      compactBtn.className = 'chat-context-compact-btn';
      compactBtn.textContent = 'Compact';
      compactBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        compactBtn.disabled = true;
        compactBtn.replaceChildren();
        try {
          const wp = spinnerModule.createWhirlpool(13);
          wp.element.style.margin = '0 5px 0 0';
          compactBtn.appendChild(wp.element);
        } catch (_) {}
        compactBtn.appendChild(document.createTextNode('Compacting'));
        const ok = await compactCurrentChatContext();
        if (!ok) {
          compactBtn.disabled = false;
          compactBtn.textContent = 'Compact failed';
        }
      });
      popup.appendChild(compactBtn);
    }

    pill.classList.add('open');
    _positionContextHeaderPopup(popup, pill);
    setTimeout(() => {
      const close = (ev) => {
        if (popup.contains(ev.target) || pill.contains(ev.target)) return;
        document.removeEventListener('pointerdown', close, true);
        _closeContextHeaderPopup();
      };
      document.addEventListener('pointerdown', close, true);
    }, 0);
  }

  function _bindContextHeaderPill() {
    if (_contextHeaderBound) return;
    _contextHeaderBound = true;
    const pill = document.getElementById('chat-context-pill');
    if (!pill) return;
    pill.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      _showContextHeaderPopup();
    });
  }

  function chatCodeBoundaryScope(chatID, accountID, privateMode=false) {
    if (privateMode || !accountID || _inputOwner!==accountID || document.body.dataset.accountId!==accountID
        || sessionModule.getCurrentSessionId()!==chatID || sessionModule.isCurrentSessionIncognito?.()
        || uiModule.el('incognito-toggle')?.checked) return null;
    const workspaceID=sessionModule.getSessions().find(row=>row.id===chatID)?.workspace_id;
    return captureCodeActivityPreference({accountID,workspaceID}) ? {accountID,workspaceID,hostChatID:chatID} : null;
  }

  function reportCodeContextBoundary(scope,boundary) {
    if (!scope || !boundary) return;
    const fresh=chatCodeBoundaryScope(scope.hostChatID,scope.accountID);
    if (fresh?.workspaceID===scope.workspaceID) publishCodeContextBoundary({...boundary,...scope});
  }

  export async function compactCurrentChatContext() {
    const sm = _liveSessionModule();
    const sid = sm && sm.getCurrentSessionId && sm.getCurrentSessionId();
    if (!sid) {
      uiModule.showToast('Open a chat first');
      return false;
    }
    const graphScope=chatCodeBoundaryScope(sid,_inputOwner);
    try {
      const res = await fetch(`/api/session/${encodeURIComponent(sid)}/compact`, { method: 'POST' });
      if (!res.ok) throw new Error(await res.text());
      const receipt=await res.json().catch(()=>null);
      if (receipt?.ok===true && receipt.source_parts_retained===true) reportCodeContextBoundary(graphScope,receipt.activity_boundary);
      uiModule.showToast('Context compacted');
      _closeContextHeaderPopup();
      if (sm && sm.selectSession) await sm.selectSession(sid, { keepSidebar: true, showLoading: false });
      refreshChatContextHeader('compact');
      return true;
    } catch (err) {
      uiModule.showError(`Compact failed: ${err.message || err}`);
      return false;
    }
  }
  try { window.compactCurrentChatContext = compactCurrentChatContext; } catch (_) {}

  export async function refreshChatContextHeader(reason = '') {
    _bindContextHeaderPill();
    const pill = document.getElementById('chat-context-pill');
    if (!pill) return;
    const sm = _liveSessionModule();
    const sid = sm && sm.getCurrentSessionId && sm.getCurrentSessionId();
    const seq = ++_contextHeaderSeq;
    if (!sid) {
      _contextHeaderData = null;
      pill.hidden = true;
      _closeContextHeaderPopup();
      return;
    }
    pill.hidden = false;
    pill.classList.add('loading');
    try {
      const res = await fetch(`/api/session/${encodeURIComponent(sid)}/context`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      if (seq !== _contextHeaderSeq) return;
      const latestSm = _liveSessionModule();
      if (!latestSm.getCurrentSessionId || latestSm.getCurrentSessionId() !== sid) return;
      _contextHeaderData = data;
      const pct = Number(data.context_percent || 0);
      _renderContextHeaderRing(pill, pct);
      _renderCompactMenuContextIcon(pct);
      pill.title = `${_fmtContextNumber(data.used_tokens)} / ${_fmtContextNumber(data.context_length)} tokens · ${String(data.model || '').split('/').pop()}`;
      pill.classList.remove('warn', 'danger');
      const colorClass = _contextColorClass(pct);
      if (colorClass) pill.classList.add(colorClass);
      pill.classList.remove('loading');
      if (pill.classList.contains('open')) {
        _closeContextHeaderPopup();
        _showContextHeaderPopup();
      }
    } catch (err) {
      if (seq !== _contextHeaderSeq) return;
      _contextHeaderData = null;
      pill.hidden = true;
      pill.classList.remove('loading', 'warn', 'danger');
      _closeContextHeaderPopup();
      console.warn('context header refresh failed:', reason, err);
    }
  }
  try { window.refreshChatContextHeader = refreshChatContextHeader; } catch (_) {}

  function _setForegroundChatBusy(active) {
    try {
      window.__odysseusChatBusy = !!active;
      window.__odysseusChatBusyUntil = active ? Date.now() + 120000 : Date.now() + 1200;
      window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', { detail: { active: !!active } }));
    } catch (_) {}
  }
  let _pendingContinue = null; // Stores the stopped AI element to merge with new response
  function _createChatSendPerf() {
    const started = (performance && performance.now) ? performance.now() : Date.now();
    let last = started;
    let reported = false;
    const stages = [];
    const now = () => (performance && performance.now) ? performance.now() : Date.now();
    return {
      mark(name) {
        const t = now();
        stages.push({ name, delta_ms: Math.round(t - last), at_ms: Math.round(t - started) });
        last = t;
      },
      report(extra) {
        if (reported) return;
        const total = Math.round(now() - started);
        const slowStage = stages.some(s => (s.delta_ms || 0) >= 1500);
        if (total < 1500 && !slowStage) return;
        reported = true;
        const payload = JSON.stringify({
          type: 'chat_send',
          total_ms: total,
          stages,
          extra: extra || '',
          session: sessionModule && sessionModule.getCurrentSessionId ? sessionModule.getCurrentSessionId() : '',
        });
        try {
          if (navigator.sendBeacon) {
            navigator.sendBeacon(`${API_BASE}/api/client-perf`, new Blob([payload], { type: 'application/json' }));
            return;
          }
        } catch (_) {}
        try {
          fetch(`${API_BASE}/api/client-perf`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
            keepalive: true,
            credentials: 'same-origin',
          }).catch(() => {});
        } catch (_) {}
      }
    };
  }

  function _hashSessionCandidate() {
    try {
      const hashId = String(window.location.hash || '').replace(/^#/, '').trim();
      if (!hashId) return '';
      if (/^(document|note|image|email|event|task|skill|research)-/.test(hashId) || /^open=notes&note=/.test(hashId)) return '';
      return hashId;
    } catch (_) {
      return '';
    }
  }

  async function _adoptOpenedSessionBeforeAutoCreate() {
    if (!sessionModule || !sessionModule.getCurrentSessionId || sessionModule.getCurrentSessionId()) return true;
    const activeRowId = document.querySelector('.list-item.active-session[data-session-id], .session-item.active[data-session-id]')?.dataset?.sessionId || '';
    const hashId = _hashSessionCandidate();
    const lastSelectedId = String(window.__odysseusLastSelectedSessionId || '').trim();
    const targetId = activeRowId || hashId || lastSelectedId;
    if (!targetId) return false;
    const adoptionNav = sessionModule.getSessionNavigationToken();
    try {
      window.__odysseusComposerUserEdited = true;
      if (sessionModule.selectSession) {
        await sessionModule.selectSession(targetId, { keepSidebar: true, showLoading: false });
      } else if (sessionModule.setCurrentSessionId) {
        sessionModule.setCurrentSessionId(targetId);
      }
      return sessionModule.getCurrentSessionId() === targetId &&
        sessionModule.getSessionNavigationToken() === adoptionNav + 1;
    } catch (_) {
      return false;
    }
  }

  // shortModel and modelColor are now in chatRenderer.js
  var _shortModel = chatRenderer.shortModel;
  var _modelRouteLabel = chatRenderer.modelRouteLabel;
  var _sameModelName = chatRenderer.sameModelName;
  var _applyModelColor = chatRenderer.applyModelColor;
  function _setRoleModelLabel(roleEl, requestedModel, actualModel, opts) {
    if (!roleEl) return;
    opts = opts || {};
    const tsSpan = roleEl.querySelector('.role-timestamp');
    const req = requestedModel || actualModel || '';
    const actual = actualModel || requestedModel || '';
    let label = _modelRouteLabel(req, actual);
    if (opts.suffix) label += ' (' + opts.suffix + ')';
    if (opts.characterName) {
      label = opts.characterName;
      // Live identity (R17): mark whether this label tracks the chat's own
      // persona or the global default, so a default-persona rename can
      // rebrand it in place.
      roleEl.dataset.agentName =
        (window.__sessionPersonaName && opts.characterName === window.__sessionPersonaName)
          ? 'session' : 'default';
    } else {
      delete roleEl.dataset.agentName;
    }
    roleEl.textContent = label + ' ';
    _applyModelColor(roleEl, actual || req);
    if (req && actual && !_sameModelName(req, actual)) {
      roleEl.title = req + ' -> ' + actual + (opts.reason ? ': ' + opts.reason : '');
    } else if (!opts.reason) {
      roleEl.removeAttribute('title');
    }
    if (tsSpan) roleEl.appendChild(tsSpan);
  }

  function _bestKnownStreamModel(routeSnapshot) {
    try {
      const current = sessionModule.getCurrentModel ? sessionModule.getCurrentModel() : '';
      if (current) return current;
    } catch (_) {}
    try {
      const pending = sessionModule.getPendingChat && sessionModule.getPendingChat();
      if (pending && pending.modelId) return pending.modelId;
    } catch (_) {}
    if (routeSnapshot && routeSnapshot.model) return routeSnapshot.model;
    try {
      const dc = window.__odysseusDefaultChat || JSON.parse(localStorage.getItem('openclank-default-chat-cache') || 'null');
      if (dc && dc.model) return dc.model;
    } catch (_) {}
    return '';
  }
  // Per-session research tracking (supports concurrent research across sessions)
  const _researchingStreamIds = new Set();
  let _researchTimerEl = null, _researchTimerInterval = null;
  let _researchStartTime = 0, _researchAvgDuration = null;
  let _researchSynapse = null;
  function _clearResearchTimer() {
    if (_researchTimerInterval) { clearInterval(_researchTimerInterval); _researchTimerInterval = null; }
    if (_researchTimerEl) { _researchTimerEl.remove(); _researchTimerEl = null; }
    if (_researchSynapse) {
      // Mark complete first so the user briefly sees the "done" state,
      // then tear it down on next tick.
      try { _researchSynapse.complete(); } catch {}
      const s = _researchSynapse;
      _researchSynapse = null;
      setTimeout(() => { try { s.destroy(); } catch {} }, 800);
    }
    _researchStartTime = 0;
    _researchAvgDuration = null;
  }

  /** Append a "Generate Visual Report" button — delegates to chatRenderer. */
  function _appendViewReportLink(msgEl, sessionId) {
    const body = msgEl.querySelector('.body');
    if (body) chatRenderer.appendReportButton(body, sessionId);
  }

  function _stripDocumentFenceForChat(text, { final = false } = {}) {
    let s = String(text || '').replace(/<?\|end\|>?/g, '');
    const markerMatch = /```(?:create_document|documen(?:t)?)\s*\n/i.exec(s);
    if (!markerMatch) return s;
    const before = s.slice(0, markerMatch.index).trimEnd();
    const fenceStart = markerMatch.index;
    const openingEnd = s.indexOf('\n', fenceStart);
    const closeIdx = openingEnd >= 0 ? s.indexOf('\n```', openingEnd + 1) : -1;
    const after = closeIdx >= 0 ? s.slice(closeIdx + 4).trimStart() : '';
    const visible = [before, after].filter(Boolean).join('\n\n').trim();
    return final && !visible ? 'Done.' : visible;
  }

  function _stripIncompleteRawToolJsonForChat(text) {
    const s = String(text || '');
    const starts = ['[{"function"', '[\n{"function"', '{"function"'];
    let idx = -1;
    for (const marker of starts) idx = Math.max(idx, s.lastIndexOf(marker));
    if (idx < 0) return s;
    const tail = s.slice(idx);
    // Complete raw OpenAI-style function blobs are removed by stripToolBlocks.
    // While the stream is still mid-JSON, hide the tail so it never flashes in
    // the chat bubble as prose.
    if (!/"type"\s*:\s*"function"/.test(tail) || !/\}\s*\]?\s*(?:<\/?\|(?:assistant|assistan|user|system|tool)\|>?)?\s*$/i.test(tail)) {
      return s.slice(0, idx);
    }
    return s;
  }

  function _streamDisplayText(text, opts = {}) {
    return stripToolBlocks(_stripIncompleteRawToolJsonForChat(_stripDocumentFenceForChat(text, opts)));
  }

  function _showDocumentWritingStatus(contentEl) {
    const msg = contentEl && contentEl.closest ? contentEl.closest('.msg') : null;
    const chatBox = document.getElementById('chat-history');
    if (!msg || !chatBox) {
      if (contentEl) contentEl.textContent = 'Writing...';
      return;
    }
    let thread = msg._docWritingThread;
    if (!thread || !thread.isConnected) {
      thread = document.createElement('div');
      thread.className = 'agent-thread streaming has-bottom';
      thread.dataset.docWriting = '1';
      const prev = msg.previousElementSibling;
      if (prev && (prev.classList.contains('msg') || prev.classList.contains('agent-thread'))) {
        thread.classList.add('has-top');
      }
      const node = document.createElement('div');
      node.className = 'agent-thread-node running';
      node.innerHTML = '<div class="agent-thread-dot"></div><div class="agent-thread-header"><span class="agent-thread-icon">▶</span><span class="agent-thread-tool">Writing</span><span class="agent-thread-wave">▁▂▃</span></div><div class="agent-thread-content"></div>';
      thread.appendChild(node);
      chatBox.insertBefore(thread, msg);
      msg._docWritingThread = thread;

      const waveEl = node.querySelector('.agent-thread-wave');
      if (waveEl) {
        const waveFrames = ['▁▂▃', '▂▃▄', '▃▄▅', '▄▅▆', '▅▆▇', '▆▅▄', '▅▄▃', '▄▃▂'];
        let waveIdx = 0;
        node._waveInterval = setInterval(() => {
          waveIdx = (waveIdx + 1) % waveFrames.length;
          waveEl.textContent = waveFrames[waveIdx];
        }, 100);
      }
      node._startTime = Date.now();
      node._elapsedTicker = setInterval(() => {
        const hdr = node.querySelector('.agent-thread-header');
        if (!hdr) return;
        let el = hdr.querySelector('.agent-thread-elapsed');
        if (!el) {
          el = document.createElement('span');
          el.className = 'agent-thread-elapsed';
          const icon = hdr.querySelector('.agent-thread-icon');
          if (icon && icon.nextSibling) hdr.insertBefore(el, icon.nextSibling);
          else hdr.appendChild(el);
        }
        const s = (Date.now() - node._startTime) / 1000;
        el.textContent = s < 60 ? `${s.toFixed(2)}s` : `${Math.floor(s / 60)}m ${(s % 60).toFixed(2).padStart(5, '0')}s`;
      }, 50);
    }
    msg.style.display = 'none';
  }

  function _finishDocumentWritingStatus(msg, ok = true) {
    const thread = msg && msg._docWritingThread;
    if (!thread || !thread.isConnected) return;
    thread.classList.remove('streaming');
    const node = thread.querySelector('.agent-thread-node');
    if (!node) return;
    if (node._waveInterval) { clearInterval(node._waveInterval); node._waveInterval = null; }
    if (node._elapsedTicker) { clearInterval(node._elapsedTicker); node._elapsedTicker = null; }
    node.classList.remove('running');
    if (!ok) node.classList.add('error');
    const icon = node.querySelector('.agent-thread-icon');
    if (icon) icon.textContent = ok ? '✓' : '✗';
    const wave = node.querySelector('.agent-thread-wave');
    if (wave) wave.remove();
    if (!node.querySelector('.agent-thread-status')) {
      const status = document.createElement('span');
      status.className = 'agent-thread-status';
      status.textContent = ok ? 'done' : 'failed';
      const header = node.querySelector('.agent-thread-header');
      if (header) header.appendChild(status);
    }
  }

  let currentAccumulated = ''; // Track accumulated text across function scope
  let currentHolder = null; // Track current message holder
  let currentSpinner = null; // Track current spinner for stop cleanup

  // Background streaming support
  const _backgroundStreams = new Map(); // sessionId -> { status, accumulated, sourcesHtml, abortCtrl, query, metrics, runId }
  const _activeStreams = new Map();     // sessionId -> { abortCtrl, holder, query, startedAt, runId }
  const _resumingStreams = new Map();   // sessionId -> detached run ID while a resumeStream() reader is live
  let _streamSessionId = null; // Session ID for the currently active reader loop
  let _lastReaderActivity = 0; // Timestamp of last reader.read() success — used to detect frozen streams
  let _webLockRelease = null;  // Function to release the Web Lock held during streaming
  let _staleStreamProbeInFlight = false;
  const STALE_LOCAL_STREAM_MS = 15000;

  const _questionRevisions = new Map();
  let _sessionStateRestoreSeq = 0;
  const _bumpQuestionRevision = sid => _questionRevisions.set(sid, (_questionRevisions.get(sid) || 0) + 1);
  const _sessionStateKey = (kind, sessionId) => `openclank-agent-${kind}-${sessionId}`;
  function _storeSessionState(kind, sessionId, value) {
    if (!sessionId || !value) return;
    if (kind === 'question') _bumpQuestionRevision(sessionId);
    try { sessionStorage.setItem(_sessionStateKey(kind, sessionId), JSON.stringify(value)); } catch (_) {}
  }
  function _clearSessionState(kind, sessionId) {
    if (kind === 'question') _bumpQuestionRevision(sessionId);
    try { sessionStorage.removeItem(_sessionStateKey(kind, sessionId)); } catch (_) {}
  }
  function _readSessionState(kind, sessionId) {
    try { return JSON.parse(sessionStorage.getItem(_sessionStateKey(kind, sessionId)) || 'null'); }
    catch (_) { return null; }
  }
  let _disposePlanFeedback = null;
  function _renderPlanDock(planState) {
    _disposePlanFeedback?.();
    _disposePlanFeedback = null;
    const dock = document.getElementById('mimo-plan-dock');
    if (!dock) return;
    const plan = planState && typeof planState.plan === 'string' ? planState.plan.trim() : '';
    const todos = Array.isArray(planState?.todos)
      ? planState.todos.filter(todo => String(todo?.content || todo?.title || todo?.text || '').trim())
      : [];
    dock.dataset.chatId = sessionModule.getCurrentSessionId() || '';
    dock.hidden = !plan && !todos.length;
    if (dock.hidden) return;
    const revision = dock.querySelector('[data-plan-revision]');
    const text = dock.querySelector('[data-plan-text]');
    const list = dock.querySelector('[data-plan-todos]');
    if (revision) { revision.hidden = !planState?.revision; revision.textContent = planState?.revision ? `Revision ${Number(planState.revision)}` : ''; }
    if (text) { text.textContent = plan; text.hidden = !plan; }
    if (list) {
      list.replaceChildren(...todos.map((todo) => {
        const item = document.createElement('li');
        item.dataset.status = String(todo.status || 'pending');
        item.textContent = String(todo.content || todo.title || todo.text || '');
        return item;
      }));
    }
    const sid = sessionModule.getCurrentSessionId(), owner = _inputOwner;
    const authEpoch = _codeEvidenceAuthEpoch, nav = sessionModule.getSessionNavigationToken();
    const scope = captureContinuityScope();
    _disposePlanFeedback = mountPlanFeedback(dock, {apiBase: API_BASE, chat: sid, plan: planState,
      captureTurnPolicy: () => _captureTurnPolicy(),
      mode: () => (_getForegroundStreamState() && !_inputQueueEnabled) ? 'steer' : 'after-turn', current: () => {
      const now = captureContinuityScope();
      return owner && _inputOwner === owner && _codeEvidenceAuthEpoch === authEpoch &&
        sessionModule.getCurrentSessionId() === sid && sessionModule.getSessionNavigationToken() === nav &&
        now.account === scope.account && now.workspace === scope.workspace && now.authEpoch === scope.authEpoch &&
        (!planState?.revision || (_readSessionState('plan', sid)?.revision === planState.revision && _readSessionState('plan', sid)?.digest === planState.digest));
    }});
  }
  function _setStoredPlan(planState, sessionId) {
    const sid = sessionId || sessionModule.getCurrentSessionId();
    if (!sid || !planState) return;
    const normalized = typeof planState === 'string' ? { plan: planState } : planState;
    _storeSessionState('plan', sid, normalized);
    if (sessionModule.getCurrentSessionId() === sid) _renderPlanDock(normalized);
  }
  document.addEventListener('odysseus:session-selected', event => {
    const session = event.detail?.sessionId;
    _renderPlanDock(session ? _readSessionState('plan', session) : null);
  });
  async function restoreSessionState(sessionId) {
    if (sessionModule.getCurrentSessionId() !== sessionId) return;
    if (!sessionId) { _renderPlanDock(null); return; }
    const owner = _inputOwner;
    const seq = ++_sessionStateRestoreSeq;
    const navToken = sessionModule.getSessionNavigationToken();
    const questionRevision = _questionRevisions.get(sessionId) || 0;
    _renderPlanDock(_readSessionState('plan', sessionId));
    const permission = _readSessionState('permission', sessionId);
    // A stored question is history, not proof of a live native callback.
    if (permission) chatRenderer.renderPermissionCard(permission, sessionId);
    try {
      const response = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/mimo-state?refresh=true`, {
        credentials: 'same-origin',
      });
      if (!response.ok) return;
      const state = response.ok ? await response.json() : null;
      if (seq !== _sessionStateRestoreSeq || sessionModule.getCurrentSessionId() !== sessionId ||
          sessionModule.getSessionNavigationToken() !== navToken) return;
      if (state?.plan_state) _setStoredPlan(state.plan_state, sessionId);
      // An older readback cannot erase a question received/resolved meanwhile.
      if (state && (_questionRevisions.get(sessionId) || 0) === questionRevision) {
        if (state.pending_question) {
          _storeSessionState('question', sessionId, state.pending_question);
          chatRenderer.renderAskUserCard(state.pending_question, { scroll: false, focus: false });
        } else {
          _clearSessionState('question', sessionId);
          document.querySelectorAll('.mimo-question-card').forEach(card => {
            if (card.dataset.sessionId === sessionId && card.dataset.interactive !== 'false'
                && card.dataset.blocking !== 'false') card.remove();
          });
        }
      }
      void _restoreOptionalQuestions(sessionId, owner, navToken, seq);
    } catch (_) {}
  }

  async function _restoreOptionalQuestions(chatID, owner, navToken, restoreSeq) {
    if (!owner || sessionModule.isCurrentSessionIncognito?.()) return;
    const deadline = Date.now() + 15000;
    try {
      const overviewResponse = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}`, { credentials: 'same-origin', signal: AbortSignal.timeout(5000) });
      if (!overviewResponse.ok) {
        if (report) throw new Error(`Chat mapping read failed (${overviewResponse.status})`);
        return;
      }
      const overview = await overviewResponse.json();
      const mapRevision = Number(overview.mapRevision);
      if (!Number.isSafeInteger(mapRevision) || _inputOwner !== owner
          || sessionModule.getCurrentSessionId() !== chatID
          || sessionModule.getSessionNavigationToken() !== navToken
          || restoreSeq !== _sessionStateRestoreSeq) return;
      const actors = Array.isArray(overview.actors) ? overview.actors.slice(0, 50) : [];
      const localReceipts = inputDelivery.readReceipts(localStorage, owner, chatID);
      for (const actor of actors) {
        if (Date.now() >= deadline || _inputOwner !== owner
            || sessionModule.getCurrentSessionId() !== chatID
            || sessionModule.getSessionNavigationToken() !== navToken
            || restoreSeq !== _sessionStateRestoreSeq) return;
        if (!actor || typeof actor.actorID !== 'string' || typeof actor.sessionID !== 'string') continue;
        const query = new URLSearchParams({ map_revision: String(mapRevision), actor_session_id: actor.sessionID, limit: '100' });
        const receiptResponse = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}/actors/${encodeURIComponent(actor.actorID)}/input?map_revision=${mapRevision}&actor_session_id=${encodeURIComponent(actor.sessionID)}`, { credentials: 'same-origin', signal: AbortSignal.timeout(5000) });
        if (receiptResponse.ok) {
          const receiptData = await receiptResponse.json();
          for (const row of receiptData.receipts || []) {
            if (!row || typeof row.requestID !== 'string' || !Array.isArray(row.questionAnswers) || !row.questionAnswers.length) continue;
            const prior = localReceipts.find(item => item.requestID === row.requestID) || {};
            const saved = inputDelivery.saveReceipt(localStorage, {
              ...prior, owner, chatID, requestID: row.requestID,
              text: typeof row.text === 'string' ? row.text : '',
              uploadIDs: Array.isArray(row.attachmentUploadIDs) ? row.attachmentUploadIDs : [],
              questionRefs: Array.isArray(row.questionRefs) ? row.questionRefs : [],
              questionAnswers: row.questionAnswers,
              actorID: actor.actorID, actorSessionID: actor.sessionID,
              mode: row.mode === 'after-turn' ? 'after-turn' : 'steer',
              state: row.state || 'persisted', queueDisposition: row.queueDisposition, deliveryState: row.state === 'consumed' ? inputDelivery.deliveryStatus({receipt: row}) : row.state === 'pending-boundary' ? 'pending-boundary' : prior.deliveryState || '',
              createdAt: Number(row.createdAt || prior.createdAt || Date.now()),
            });
            localReceipts.push(saved);
          }
        }
        const response = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}/actors/${encodeURIComponent(actor.actorID)}/questions?${query}`, { credentials: 'same-origin', signal: AbortSignal.timeout(5000) });
        if (!response.ok) continue;
        const page = await response.json();
        if (_inputOwner !== owner || sessionModule.getCurrentSessionId() !== chatID
            || sessionModule.getSessionNavigationToken() !== navToken
            || restoreSeq !== _sessionStateRestoreSeq) return;
        for (const row of page.items || []) {
          if (!row || row.blocking !== false || !Array.isArray(row.questions)) continue;
          const saved = localReceipts.find(item => item.questionRefs?.includes(row.questionRef)
            && item.actorID === row.actorID && item.actorSessionID === row.sessionID);
          const matchingAnswers = saved && Array.isArray(saved.questionAnswers)
            ? saved.questionAnswers.filter(answer => answer && answer.requestID === row.requestID
                && answer.questionRevision === row.questionRevision
                && row.questionRef === `${answer.requestID}@${answer.questionRevision}`)
            : [];
          const savedAnswer = matchingAnswers.length === 1 ? matchingAnswers[0] : undefined;
          const payload = {
            request_id: row.questionRef,
            session_id: chatID,
            native_request_id: row.requestID,
            actor_id: row.actorID,
            actor_session_id: row.sessionID,
            origin_turn_id: row.originTurnID,
            question_revision: row.questionRevision,
            question_ref: row.questionRef,
            blocking: false,
            questions: row.questions,
            answer_state: row.state,
            ...(savedAnswer ? { saved_answer: savedAnswer } : {}),
            ...(saved ? { local_delivery_state: savedAnswer ? saved.deliveryState : 'unavailable' } : {}),
          };
          chatRenderer.renderAskUserCard(payload, { scroll: false, focus: false });
        }
      }
    } catch (error) {
      console.warn('Could not restore optional actor questions:', error);
    }
  }

  /** Check if an SSE reader is still actively connected for a session. */
  function hasActiveStream(sessionId) {
    return _activeStreams.has(sessionId) || _streamSessionId === sessionId || _backgroundStreams.has(sessionId) ||
           _resumingStreams.has(sessionId);
  }

  function _getForegroundStreamState() {
    try {
      const sid = sessionModule && sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId();
      return sid ? (_activeStreams.get(sid) || null) : null;
    } catch (_) {
      return null;
    }
  }

  // A detached run can be replaced before a delayed Stop or timeout fires.
  // Send the identity received with the stream when present, so an old browser
  // controller cannot cancel the session's newer run. Old servers and direct
  // compare streams do not issue an identity, and retain the unheaded stop
  // compatibility lane.
  function _stopDetachedRun(sessionId) {
    if (!sessionId) return;
    const active = _activeStreams.get(sessionId);
    const background = _backgroundStreams.get(sessionId);
    const runId = (active && active.runId) || (background && background.runId) ||
      _resumingStreams.get(sessionId) || '';
    const headers = runId ? { 'X-Agent-Run-ID': runId } : undefined;
    fetch(`/api/chat/stop/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers,
    }).catch(() => {});
  }

  let _suggestionController = null;
  let _suggestionSeen = '';
  let _suggestionNode = null;
  function _cancelSuggestion() {
    if (_suggestionController) _suggestionController.abort();
    _suggestionController = null;
    if (_suggestionNode) _suggestionNode.remove();
    _suggestionNode = null;
  }
  async function _suggestWhenIdle() {
    const input = uiModule?.el('message');
    const sid = sessionModule?.getCurrentSessionId?.();
    const owner = _inputOwner;
    if (_suggestionController || !sid || !owner || !input || input.value || isStreaming || _sendInFlight || uiModule.el('incognito-toggle')?.checked) return;
    _cancelSuggestion();
    const controller = new AbortController(); _suggestionController = controller;
    const same = () => _suggestionController === controller && !controller.signal.aborted && _inputOwner === owner &&
      sessionModule.getCurrentSessionId() === sid && !input.value && !isStreaming && !_sendInFlight;
    try {
      const base = `${API_BASE}/api/session/${encodeURIComponent(sid)}/next-prompt`;
      const read = await fetch(base, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!read.ok || !same()) return;
      const view = await read.json();
      if (view.state !== 'selected' || !view.revision || _suggestionSeen === view.revision || !same()) return;
      _suggestionSeen = view.revision;
      const info = document.createElement('div'); info.className = 'settings-hint';
      info.textContent = `Next-prompt suggestion uses ${view.selection.model}; thinking ${JSON.stringify(view.selection.thinking)}; up to ${view.selection.max_output_tokens} output tokens; billed cost unknown.`;
      input.parentNode?.appendChild(info); _suggestionNode = info;
      await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
      if (!same()) return;
      const response = await fetch(base, {method:'POST', credentials:'same-origin', signal:controller.signal,
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:view.revision})});
      if (!response.ok || !same()) return;
      const result = await response.json();
      if (!same() || result.revision !== view.revision) return;
      if (result.state === 'no_suggestion') { info.append(' No suggestion was produced.'); return; }
      if (result.state !== 'ready' || !result.suggestion) return;
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `Use suggestion: ${result.suggestion}`;
      button.addEventListener('click', () => { if (!same()) return; input.value = result.suggestion; _cancelSuggestion(); input.dispatchEvent(new Event('input', {bubbles:true})); input.focus(); });
      info.append(' ', button);
    } catch (_) { /* unavailable or cancelled; never reissue this revision */ }
  }

  function _syncForegroundStreamGlobals() {
    const active = _getForegroundStreamState();
    isStreaming = !!active;
    currentAbort = active ? active.abortCtrl : null;
    currentHolder = active ? active.holder : null;
    _setForegroundChatBusy(!!active || !!_sendInFlight);
    if (active || _sendInFlight) _cancelSuggestion(); else void _suggestWhenIdle();
    return active;
  }

  function _touchStreamActivity(sessionId) {
    const now = Date.now();
    _lastReaderActivity = now;
    const active = sessionId ? _activeStreams.get(sessionId) : null;
    if (active) active.lastActivity = now;
    return now;
  }

  // Sources box builder and toggleSources are now in chatRenderer.js
  var _buildSourcesBox = chatRenderer.buildSourcesBox;

  // Browser notifications now in chatStream.js
  var _notifyResearchComplete = chatStream.notifyResearchComplete;

  // Model/image pricing, _buildImageBubble now in chatRenderer.js
  var _buildImageBubble = chatRenderer.buildImageBubble;
  var getModelCost = chatRenderer.getModelCost;
  var getImageCost = chatRenderer.getImageCost;

  function _appendGeneratedImageBubble(data) {
    const imageUrl = data?.image_url || data?.url || '';
    if (!imageUrl) return false;
    const chatBox = document.getElementById('chat-history');
    if (!chatBox) return false;
    const imageKey = String(data.image_id || imageUrl);
    const exists = Array.from(chatBox.querySelectorAll('.generated-image-wrap')).some(el => (
      el.dataset.imageKey === imageKey ||
      el.dataset.imageUrl === imageUrl ||
      el.querySelector('img.generated-image')?.getAttribute('src') === imageUrl
    ));
    if (exists) return false;
    const bubble = _buildImageBubble(
      imageUrl,
      data.image_prompt,
      data.image_model,
      data.image_size,
      data.image_quality,
      data.image_id
    );
    bubble.dataset.imageKey = imageKey;
    bubble.dataset.imageUrl = imageUrl;
    chatBox.appendChild(bubble);
    uiModule.scrollHistory();
    window.dispatchEvent(new CustomEvent('imps-refresh'));
    return true;
  }

  // stripToolBlocks and roleTimestamp now in chatRenderer.js
  var stripToolBlocks = chatRenderer.stripToolBlocks;

  /**
   * Initialize with dependencies
   */
  export function init(apiBase) {
    API_BASE = apiBase;
    if (!_authContextSeen) {
      const bootOwner = String(document.body?.dataset?.accountId || globalThis.currentAccountId || '');
      if (bootOwner !== _inputOwner) {
        const previousOwner = _inputOwner;
        _inputOwner = bootOwner;
        if (previousOwner && previousOwner !== bootOwner) inputDelivery.clearOwnerReceipts(localStorage, previousOwner);
      }
    }
    _wireInputQueuePreference();
    _wireThinkingSelector();
    uiModule.el('message')?.addEventListener('input', _cancelSuggestion);
    initSlashCommands({ apiBase, isStreaming: () => !!_getForegroundStreamState(),
      captureRecoveryTurnPolicy: (sid) => {
        if (!sid || sid !== sessionModule.getCurrentSessionId()) throw new Error('Chat changed');
        const mode = String(Storage.loadToggleState?.()?.chat_mode || 'agent');
        if (!['agent', 'chat', 'plan'].includes(mode)) throw new Error('This recovery mode is unavailable.');
        return _captureTurnPolicy();
      },
    });
    document.addEventListener('odysseus:session-selected', (event) => {
      _cancelSuggestion(); _suggestionSeen = '';
      const selected = String(event.detail?.sessionId || sessionModule.getCurrentSessionId() || '');
      _resetComposerQueue();
      if (selected) void _loadInputReceipts(selected);
      void _loadThinkingSelection(selected);
    });
    document.addEventListener('openclank:auth-context-changed', (event) => {
      _cancelSuggestion(); _suggestionSeen = '';
      _authContextSeen = true;
      const previousOwner = _inputOwner;
      _inputOwner = String(event.detail?.accountId || '');
      if (previousOwner && previousOwner !== _inputOwner) inputDelivery.clearOwnerReceipts(localStorage, previousOwner);
      _wireInputQueuePreference();
      _resetComposerQueue();
      const selected = sessionModule.getCurrentSessionId();
      if (selected) void _loadInputReceipts(selected);
      void _loadThinkingSelection(selected);
    });
    const reloadComposerQueue = () => {
      _resetComposerQueue();
      const selected = sessionModule.getCurrentSessionId();
      if (selected) void _loadInputReceipts(selected);
    };
    window.addEventListener('workspace-change', reloadComposerQueue);
    uiModule.el('incognito-toggle')?.addEventListener('change', reloadComposerQueue);
    // Initialize email inbox
    emailInbox.init(documentModule);
    document.addEventListener('odysseus:interactive-resolved', (event) => {
      const detail = event.detail || {};
      if (!detail.sessionId || !['question','permission'].includes(detail.kind) || !detail.requestId) return;
      const current = _readSessionState(detail.kind, detail.sessionId);
      if (!current || (current.request_id !== detail.requestId &&
          (detail.kind !== 'question' || current.native_request_id !== detail.requestId))) return;
      _clearSessionState(detail.kind, detail.sessionId);
    });
    // Wire the slash-command autocomplete popup on the chat composer. The
    // dispatcher already handles the typed command — this just surfaces the
    // registry as a discoverable menu when the user starts a message with /.
    import('./slashAutocomplete.js').then(mod => {
      const ta = document.getElementById('message');
      if (ta && mod.initSlashAutocomplete) mod.initSlashAutocomplete(ta);
    }).catch(() => {});

    // ArrowUp on the composer recalls previous user prompts from this chat.
    const _wireArrowUpRecall = (composer) =>
      wireArrowUpRecall(composer, () => getUserMessagesFromChatHistory(), {
        autoResize: uiModule?.autoResize,
      });

    const composer = document.getElementById('message');
    if (!_wireArrowUpRecall(composer)) {
      // Init can run before #message exists (templated UI); short retries only.
      try { requestAnimationFrame(() => _wireArrowUpRecall(document.getElementById('message'))); } catch (_) {}
      setTimeout(() => _wireArrowUpRecall(document.getElementById('message')), 250);
    }
    void _loadThinkingSelection(sessionModule.getCurrentSessionId());
  }

  // addMessage, createMsgFooter, displayMetrics, hideWelcomeScreen, showWelcomeScreen
  // are now in chatRenderer.js — referenced via the public API delegation above.
  var addMessage = chatRenderer.addMessage;
  var createMsgFooter = chatRenderer.createMsgFooter;
  var displayMetrics = chatRenderer.displayMetrics;
  var hideWelcomeScreen = chatRenderer.hideWelcomeScreen;
  var showWelcomeScreen = chatRenderer.showWelcomeScreen;

  /**
   * Update submit button state
   */
  function updateSubmitButton(state, submitBtn) {
    if (!submitBtn) return;

    if (state === 'streaming') {
      // Clear any pending transitions from + → arrow swap
      submitBtn.classList.remove('anim-spin', 'anim-spin-swap', 'anim-land', 'mic-mode', 'newchat-mode', 'newchat-expanded', 'recording');
      // Ensure arrow icon is showing before launch
      var icons = window._odysseusBtnIcons;
      if (icons) submitBtn.innerHTML = icons.send;
      void submitBtn.offsetWidth;
      // Arrow launches up, then stop icon lands in
      submitBtn.classList.add('anim-launch');
      const _stopSvg = uiIcon("stop", 14);
      // Wait for the launch keyframe to finish (0.3s) before swapping the
      // arrow out for the stop icon — otherwise the swap happens mid-flight
      // and the user sees nothing fly out.
      setTimeout(() => {
        if (submitBtn.dataset.mode !== 'streaming') return;
        const msgInput = uiModule.el('message');
        const hasQueuedText = !!(msgInput && msgInput.value && msgInput.value.trim());
        submitBtn.innerHTML = hasQueuedText && icons ? icons.send : _stopSvg;
        submitBtn.dataset.phase = hasQueuedText ? 'queue' : 'processing';
        submitBtn.title = hasQueuedText ? 'Send follow-up (Ctrl+Enter to queue)' : 'Stop generation';
        submitBtn.classList.remove('anim-launch');
        void submitBtn.offsetWidth;
        submitBtn.classList.add('anim-land');
        submitBtn.addEventListener('animationend', () => submitBtn.classList.remove('anim-land'), { once: true });
      }, 300);
      submitBtn.title = 'Stop generation';
      submitBtn.dataset.mode = 'streaming';
      submitBtn.dataset.phase = 'processing';
      isStreaming = true;
      _setForegroundChatBusy(true);
      _startStallWatchdog();
    } else if (state === 'idle') {
      submitBtn.dataset.mode = '';
      delete submitBtn.dataset.phase;
      submitBtn.classList.remove('recording');
      isStreaming = false;
      _setForegroundChatBusy(false);
      _stopStallWatchdog();
      // Defer to global updater which handles mic/newchat/send modes
      if (window._updateSendBtnIcon) {
        setTimeout(window._updateSendBtnIcon, 50);
      } else {
        var icons = window._odysseusBtnIcons;
        submitBtn.innerHTML = icons ? icons.send : uiIcon("up", 16);
        submitBtn.title = 'Send message';
        submitBtn.classList.remove('mic-mode', 'newchat-mode');
      }
    }
  }

  // -----------------------------------------------------------------------
  // Slash commands — now in slashCommands.js
  // -----------------------------------------------------------------------

  // API key pattern for the guard in handleChatSubmit
  const API_KEY_RE = /^(sk-[a-zA-Z0-9_\-]{20,}|gsk_[a-zA-Z0-9]{20,}|AIza[a-zA-Z0-9_\-]{30,}|xai-[a-zA-Z0-9]{20,})$/;
  let _inputQueueEnabled = false;
  let _inputOwner = String(document.body?.dataset?.accountId || globalThis.currentAccountId || '');
  let _thinkingOptionModel = null;
  let _thinkingSelection = null;
  let _thinkingLoadEpoch = 0;
  let _thinkingFetchSeq = 0;
  let _thinkingScopeKey = '';
  let _thinkingSaveSeq = 0;
  let _thinkingSaveError = null;
  let _authContextSeen = false;
  let _thinkingSavePromise = Promise.resolve();

  function _extractPlanText(text) {
    const raw = String(text || '').trim();
    if (!raw) return '';
    const stripped = raw
      .replace(/<think[\s\S]*?<\/think>/gi, '')
      .replace(/<thought[\s\S]*?<\/thought>/gi, '')
      .trim();
    const lines = stripped.split('\n');
    const firstChecklist = lines.findIndex(line => /^\s*(?:[-*]|\d+\.)\s+\[[ x-]\]\s+/i.test(line));
    if (firstChecklist >= 0) return lines.slice(firstChecklist).join('\n').trim();
    const firstPlanHeading = lines.findIndex(line => /^\s{0,3}#{1,4}\s+.*plan/i.test(line) || /^\s*(?:plan|proposed plan)\s*:?$/i.test(line));
    if (firstPlanHeading >= 0) return lines.slice(firstPlanHeading).join('\n').trim();
    return stripped;
  }

	  function _attachPlanActions(target, plan) {
	    if (!target || !String(plan || '').trim() || target.querySelector('.plan-inline-actions')) return;
	    const actions = document.createElement('div');
	    actions.className = 'plan-inline-actions';
	    actions.innerHTML = `
	      <button type="button" class="plan-inline-execute">
	        ${uiIcon("play", 12)}
	        Execute
	      </button>
	      <button type="button" class="plan-inline-clear">Clear</button>`;
	    actions.querySelector('.plan-inline-execute')?.addEventListener('click', async () => {
	      const sid = sessionModule.getCurrentSessionId();
	      const current = sid ? _readSessionState('plan', sid) : null;
	      if (!sid || !current || !current.revision || !current.digest) {
	        uiModule.showError('This plan has not been persisted yet. Wait for the plan state to sync, then try Execute again.');
	        return;
	      }
	      const button = actions.querySelector('.plan-inline-execute');
	      if (button) button.disabled = true;
	      try {
	        const response = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/plan/approve`, {
	          method: 'POST',
	          credentials: 'same-origin',
	          headers: { 'Content-Type': 'application/json' },
	          body: JSON.stringify({ revision: Number(current.revision), digest: String(current.digest) }),
	        });
	        const payload = await response.json().catch(() => ({}));
	        if (!response.ok || !payload.plan_state) throw new Error(payload.detail || 'Plan approval was rejected');
	        _setStoredPlan(payload.plan_state, sid);
	        if (window.__odysseusSetPlanMode) window.__odysseusSetPlanMode(false);
	        if (window.__odysseusSetChatMode) window.__odysseusSetChatMode('agent');
	        _setComposerAndSend('Execute the approved plan.');
	      } catch (error) {
	        uiModule.showError(error?.message || 'Plan approval failed; the plan was not executed.');
	      } finally {
	        if (button) button.disabled = false;
	      }
	    });
	    actions.querySelector('.plan-inline-clear')?.addEventListener('click', () => {
	      const sid = sessionModule.getCurrentSessionId();
	      if (!sid) return;
	      fetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/plan/clear`, {
	        method: 'POST', credentials: 'same-origin',

	      }).then(async response => {
	        const payload = await response.json().catch(() => ({}));
	        if (!response.ok) throw new Error(payload.detail || 'Plan clear failed');
	        _setStoredPlan(payload.plan_state || { status: 'cleared' }, sid);
	        actions.remove();
	      }).catch(error => uiModule.showError(error?.message || 'Plan clear failed'));
	    });
	    (target.querySelector('.body') || target).appendChild(actions);
	  }

  let _inputQueuePreferenceSequence = 0;

  function _wireInputQueuePreference() {
    const field = document.getElementById('settings-input-queue');
    const account = _inputOwner;
    const sequence = ++_inputQueuePreferenceSequence;
    _inputQueueEnabled = false;
    if (field) {
      field.checked = false;
      field.disabled = true;
      field.onchange = async () => {
        const desired = field.checked;
        field.disabled = true;
        try {
          const response = await fetch(`${API_BASE}/api/prefs/input_queue_enabled`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({value: desired, expected_account_id: account}),
          });
          if (!response.ok) throw new Error('Preference update failed');
          const data = await response.json();
          if (sequence === _inputQueuePreferenceSequence && _inputOwner === account && data.account_id === account) {
            _inputQueueEnabled = data.value === true;
          }
        } catch (_) {
          if (sequence === _inputQueuePreferenceSequence && _inputOwner === account) {
            uiModule.showError('Could not save queue preference.');
          }
        } finally {
          if (sequence === _inputQueuePreferenceSequence && _inputOwner === account) {
            field.checked = _inputQueueEnabled;
            field.disabled = false;
          }
        }
      };
    }
    if (!account) return;
    fetch(`${API_BASE}/api/prefs/input_queue_enabled`).then(response => {
      if (!response.ok) throw new Error('Preference read failed');
      return response.json();
    }).then(data => {
      if (sequence === _inputQueuePreferenceSequence && _inputOwner === account && data.account_id === account) {
        _inputQueueEnabled = data.value === true;
      }
    }).catch(() => {}).finally(() => {
      if (field && sequence === _inputQueuePreferenceSequence && _inputOwner === account) {
        field.checked = _inputQueueEnabled;
        field.disabled = false;
      }
    });
  }

  function _wireThinkingSelector() {
    const host = document.querySelector('.chat-input-left');
    if (!host || document.getElementById('chat-thinking-select')) return;
    const label = document.createElement('label');
    label.className = 'chat-thinking-control';
    label.hidden = true;
    label.htmlFor = 'chat-thinking-select';
    label.append(document.createTextNode('Thinking '));
    const select = document.createElement('select');
    select.id = 'chat-thinking-select';
    select.setAttribute('aria-label', 'Thinking effort');
    label.appendChild(select);
    host.appendChild(label);
    select.addEventListener('change', async () => {
      if (!_thinkingOptionModel) return;
      const prior = _thinkingSelection;
      const scope = {
        owner: _inputOwner,
        chatID: sessionModule.getCurrentSessionId(),
        privateMode: !!uiModule.el('incognito-toggle')?.checked,
        routeID: _thinkingOptionModel.optionRouteId,
        epoch: _thinkingLoadEpoch,
      };
      const saveSeq = ++_thinkingSaveSeq;
      const stillCurrent = () => saveSeq === _thinkingSaveSeq
        && scope.owner === _inputOwner
        && scope.chatID === sessionModule.getCurrentSessionId()
        && scope.privateMode === !!uiModule.el('incognito-toggle')?.checked
        && scope.epoch === _thinkingLoadEpoch
        && scope.routeID === _thinkingOptionModel?.optionRouteId;
      select.disabled = true;
      _thinkingSaveError = null;
      _thinkingSavePromise = (async () => {
       let timeoutId;
       const controller = new AbortController();
       const deadline = new Promise((_, reject) => {
        timeoutId = setTimeout(() => {
          controller.abort();
          reject(new Error('Thinking preference save timed out. Retry before sending.'));
        }, 15000);
       });
       try {
        const selection = serializeReasoningEffortSelection(_thinkingOptionModel, select.value);
        const response = await Promise.race([fetch(`${API_BASE}/api/session/${encodeURIComponent(scope.chatID)}/model-options`, {
          method: 'PATCH', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ selection }), signal: controller.signal,
        }), deadline]);
        const result = await Promise.race([response.json(), deadline]);
        if (!response.ok) throw new Error(result.detail || `Thinking preference could not be saved (${response.status})`);
        if (stillCurrent()) _thinkingSelection = result.selection;
       } catch (error) {
        if (stillCurrent()) {
          _thinkingSaveError = error;
          _thinkingSelection = prior;
          select.value = prior?.options?.reasoning_effort || 'inherit';
          uiModule.showError(error?.message || 'Thinking preference could not be saved.');
        }
       } finally {
        clearTimeout(timeoutId);
        if (stillCurrent()) select.disabled = false;
       }
      })();
      await _thinkingSavePromise;
    });
    uiModule.el('incognito-toggle')?.addEventListener('change', () => { void _loadThinkingSelection(sessionModule.getCurrentSessionId()); });
  }

  async function _loadThinkingSelection(chatID) {
    const privateMode = !!uiModule.el('incognito-toggle')?.checked;
    const scopeKey = `${_inputOwner}\u0000${chatID || ''}\u0000${privateMode}`;
    if (scopeKey !== _thinkingScopeKey) {
      _thinkingScopeKey = scopeKey;
      ++_thinkingLoadEpoch;
      ++_thinkingSaveSeq;
      _thinkingSavePromise = Promise.resolve();
      _thinkingSaveError = null;
      _thinkingOptionModel = null;
      _thinkingSelection = null;
    }
    const epoch = _thinkingLoadEpoch;
    const fetchSeq = ++_thinkingFetchSeq;
    const saveSeqAtStart = _thinkingSaveSeq;
    const label = document.querySelector('.chat-thinking-control');
    if (!label || !chatID || privateMode !== !!sessionModule.isCurrentSessionIncognito?.()) {
      if (label) label.hidden = true;
      return;
    }
    label.hidden = true;
    const currentSelect = label.querySelector('select');
    if (currentSelect) currentSelect.disabled = false;
    try {
      const response = await fetch(`${API_BASE}/api/session/${encodeURIComponent(chatID)}/model-options`, { credentials: 'same-origin' });
      if (!response.ok) return;
      const payload = await response.json();
      if (fetchSeq !== _thinkingFetchSeq || epoch !== _thinkingLoadEpoch
          || saveSeqAtStart !== _thinkingSaveSeq
          || _inputOwner !== scopeKey.split('\u0000')[0]
          || sessionModule.getCurrentSessionId() !== chatID
          || privateMode !== !!uiModule.el('incognito-toggle')?.checked
          || privateMode !== !!sessionModule.isCurrentSessionIncognito?.()) return;
      const descriptors = Array.isArray(payload.descriptors) ? payload.descriptors : [];
      if (!descriptors.length) return;
      if (_thinkingOptionModel && (_thinkingOptionModel.optionRouteId !== payload.route_id || _thinkingOptionModel.catalogRevision !== payload.catalog_revision)) {
        ++_thinkingLoadEpoch;
        ++_thinkingSaveSeq;
        _thinkingSavePromise = Promise.resolve();
        _thinkingSaveError = null;
        _thinkingSelection = null;
      }
      _thinkingOptionModel = {
        optionRouteId: payload.route_id,
        catalogRevision: payload.catalog_revision,
        capabilities: { request_options: descriptors },
      };
      _thinkingSelection = privateMode ? _thinkingSelection : (payload.selection || null);
      const select = label.querySelector('select');
      const descriptor = descriptors.find(item => item.id === 'reasoning_effort');
      if (!select || !descriptor) return;
      select.replaceChildren(new Option('Default', 'inherit'));
      for (const effort of descriptor.values || []) select.appendChild(new Option(effort, effort));
      const restored = restoreReasoningEffortSelection(_thinkingOptionModel, _thinkingSelection);
      if (restored.status === 'stale') {
        _thinkingSelection = null;
        uiModule.showToast?.('Thinking options changed. Choose an effort again.');
      }
      select.value = restored.value || 'inherit';
      label.hidden = false;
    } catch (error) {
      // Hide the control when the host cannot authorize this route.
      console.warn('Could not load Thinking options:', error);
    }
  }

  let _inputReceiptRead = 0;

  function _durableInputScope() {
    const scope = captureContinuityScope();
    if (!scope.account || scope.account !== _inputOwner || !scope.chat
        || sessionModule.isCurrentSessionIncognito?.() || uiModule.el('incognito-toggle')?.checked) return null;
    return { ...scope, navigation: sessionModule.getSessionNavigationToken() };
  }

  function _durableInputScopeCurrent(scope) {
    const now = _durableInputScope();
    return !!scope && !!now && scope.account === now.account && scope.authEpoch === now.authEpoch
      && scope.chat === now.chat && scope.workspace === now.workspace && scope.navigation === now.navigation;
  }

  function _createComposerQueue() {
    const attachments = uiModule.el('attach-strip');
    if (!attachments) return null;
    const panel = document.createElement('details');
    panel.id = 'chat-composer-queue';
    panel.className = 'chat-composer-queue';
    panel.hidden = true;
    const summary = document.createElement('summary');
    const refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.textContent = 'Refresh status';
    refresh.addEventListener('click', () => _refreshComposerQueue(true));
    const rows = document.createElement('div');
    rows.className = 'chat-composer-queue-rows';
    panel.append(summary, refresh, rows);
    attachments.before(panel);
    return panel;
  }

  function _updateComposerQueue(panel) {
    const count = panel.lastElementChild.childElementCount;
    panel.firstElementChild.textContent = `${count} pending follow-up${count === 1 ? '' : 's'}`;
    panel.hidden = count === 0;
  }

  function _resetComposerQueue() {
    _inputReceiptRead += 1;
    document.getElementById('chat-composer-queue')?.remove();
    document.querySelectorAll('#chat-history .chat-input-receipt').forEach(card => card.remove());
  }

  function _refreshComposerQueue(report = false) {
    const panel = document.getElementById('chat-composer-queue');
    const scope = _durableInputScope();
    if (scope && panel && !panel.hidden) void _loadInputReceipts(scope.chat, report);
  }

  function _renderInputReceipt(receipt) {
    const scope = _durableInputScope();
    if (!scope || receipt.owner !== scope.account || receipt.chatID !== scope.chat) return;
    const card = chatRenderer.renderInputReceipt(receipt, {
      onRetry: (item) => _submitDurableInput(item),
      onSendNow: (item) => _submitDurableInput({ ...item, mode: 'steer', handoffID: inputDelivery.createRequestID() }),
      onCancel: (item) => _cancelDurableInput(item),
    });
    if (!card || (receipt.actorID && receipt.actorID !== 'main')) return;
    const pending = receipt.state !== 'cancelled'
      && !['cancelled', 'queue-delivered'].includes(receipt.deliveryState);
    let panel = document.getElementById('chat-composer-queue');
    if (pending) panel ||= _createComposerQueue();
    if (!panel) return;
    const rows = panel.lastElementChild;
    if (pending && card.parentElement !== rows) rows.append(card);
    if (!pending && card.parentElement === rows) document.getElementById('chat-history')?.append(card);
    _updateComposerQueue(panel);
  }

  async function _loadInputReceipts(chatID, report = false) {
    const scope = _durableInputScope();
    if (!scope || chatID !== scope.chat) return;
    const owner = scope.account;
    const read = ++_inputReceiptRead;
    const current = () => read === _inputReceiptRead && _durableInputScopeCurrent(scope);
    try {
      const local = inputDelivery.readReceipts(localStorage, owner, chatID);
      if (!report) local.filter(item => !item.questionAnswers?.length)
        .sort((a, b) => a.createdAt - b.createdAt).forEach(_renderInputReceipt);
      const overviewResponse = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}`, { credentials: 'same-origin' });
      if (!overviewResponse.ok) return;
      const overview = await overviewResponse.json();
      if (!current()) return;
      const mapRevision = Number(overview.mapRevision);
      if (!Number.isSafeInteger(mapRevision)) return;
      const response = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}/actors/main/input?map_revision=${mapRevision}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`Receipt read failed (${response.status})`);
      const payload = await response.json();
      if (!current()) return;
      if (payload.actorSessionId !== overview.engineSessionId || !payload.actorSessionId) return;
      const localNow = inputDelivery.readReceipts(localStorage, owner, chatID);
      const byID = new Map(localNow.map(item => [item.requestID, item]));
      for (const row of payload.receipts || []) {
        if (!row || typeof row.requestID !== 'string') continue;
        const prior = byID.get(row.requestID) || {};
        const item = inputDelivery.saveReceipt(localStorage, {
          ...prior, owner, chatID, requestID: row.requestID,
          text: typeof row.text === 'string' ? row.text : prior.text || '',
          uploadIDs: Array.isArray(row.attachmentUploadIDs) ? row.attachmentUploadIDs : prior.uploadIDs || [],
          mode: row.mode === 'after-turn' ? 'after-turn' : 'steer',
          state: row.state || prior.state || 'persisted',
          queueDisposition: row.queueDisposition,
          deliveryState: row.state === 'consumed' ? inputDelivery.deliveryStatus({receipt: row}) : row.state === 'pending-boundary' ? 'pending-boundary' : prior.deliveryState || '',
          deliveryReason: row.failureReason || prior.deliveryReason,
          handoffID: prior.handoffID,
          modelOptions: prior.modelOptions,
          turnPolicy: row.turnPolicy ?? prior.turnPolicy,
          questionRefs: Array.isArray(row.questionRefs) ? row.questionRefs : prior.questionRefs,
          questionAnswers: Array.isArray(row.questionAnswers) ? row.questionAnswers : prior.questionAnswers,
          actorID: 'main', actorSessionID: payload.actorSessionId || overview.engineSessionId,
          ...(Number.isSafeInteger(row.selectedExecutionRevision) && Number.isSafeInteger(row.mapRevision)
            ? { expectedExecutionRevision: row.selectedExecutionRevision, expectedMapRevision: row.mapRevision } : {}),
          createdAt: Number(row.createdAt || prior.createdAt || Date.now()),
        });
        byID.set(item.requestID, item);
      }
      [...byID.values()].filter(item => !item.questionAnswers?.length)
        .sort((a, b) => a.createdAt - b.createdAt).forEach(_renderInputReceipt);
    } catch (error) {
      if (report && current()) uiModule.showError(error.message || 'Follow-up status is unavailable.');
      console.warn('Could not restore actor input receipts:', error);
    }
  }

  async function _submitDurableInput(receipt) {
    const owner = _inputOwner;
    const chatID = sessionModule.getCurrentSessionId();
    if (!owner || !chatID || receipt.owner !== owner || receipt.chatID !== chatID) throw new Error('This follow-up belongs to a different signed-in chat.');
    const selectedScope = _durableInputScope();
    if (!selectedScope) throw new Error('This follow-up requires its signed-in, saved chat.');
    const selectedCurrent = () => _durableInputScopeCurrent(selectedScope);
    _inputReceiptRead += 1;
    const unavailable = message => Object.assign(new Error(message), { definiteInputPolicyRefusal: true, reason: message });
    const request = inputDelivery.saveReceipt(localStorage, {
      ...receipt,
      owner,
      chatID,
      state: receipt.state || 'persisted',
      deliveryState: receipt.deliveryState || 'unknown',
    });
    if (!request.questionAnswers?.length) _renderInputReceipt(request);
    const overviewResponse = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}`, { credentials: 'same-origin' });
    if (!overviewResponse.ok) throw new Error(`Chat mapping read failed (${overviewResponse.status})`);
    const overview = await overviewResponse.json();
    const mapRevision = Number(overview.mapRevision);
    if (!Number.isSafeInteger(mapRevision)) throw new Error('Chat mapping revision is unavailable.');
    if (!selectedCurrent()) throw unavailable('The selected chat or account changed before admission.');
    if (request.expectedMapRevision !== undefined && mapRevision !== request.expectedMapRevision) {
      inputDelivery.saveReceipt(localStorage, { ...request, deliveryState: 'unavailable', deliveryReason: 'The selected chat mapping changed before admission.' });
      throw unavailable('The selected chat mapping changed before admission.');
    }
    const body = {
      requestID: request.requestID,
      text: request.text,
      uploadIDs: request.uploadIDs,
      attachmentRefs: [],
      questionRefs: request.questionRefs || [],
      ...(request.questionAnswers ? { questionAnswers: request.questionAnswers } : {}),
      ...(request.actorSessionID ? { actorSessionId: request.actorSessionID } : {}),
      ...(request.expectedExecutionRevision !== undefined ? { expectedExecutionRevision: request.expectedExecutionRevision } : {}),
      mode: request.mode,
      ...(request.handoffID ? { handoffID: request.handoffID } : {}),
      ...(request.modelOptions ? { modelOptions: request.modelOptions } : {}),
      ...(request.turnPolicy ? { turnPolicy: request.turnPolicy } : {}),
    };
    try {
      const actorID = request.actorID || 'main';
      const response = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}/actors/${encodeURIComponent(actorID)}/input?map_revision=${mapRevision}`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!selectedCurrent()) throw unavailable('The selected chat or account changed while the request was in flight.');
      _inputReceiptRead += 1;
      if (!response.ok) {
        const error = new Error(payload.detail?.message || payload.detail || `Follow-up was not accepted (${response.status})`);
        error.definiteInputPolicyRefusal = ['INPUT_POLICY_UNAVAILABLE', 'ACTOR_EXECUTION_CHANGED', 'INPUT_ADMISSION_REFUSED', 'INPUT_RECEIPT_CONFLICT', 'STALE_MAP'].includes(payload.detail?.code);
        if (error.definiteInputPolicyRefusal) error.reason = error.message;
        throw error;
      }
      const status = inputDelivery.deliveryStatus(payload);
      const updated = inputDelivery.saveReceipt(localStorage, {
        ...request,
        state: payload.receipt?.state || request.state,
        queueDisposition: payload.receipt?.queueDisposition,
        deliveryState: payload.receipt?.state === 'consumed' ? status : payload.deliveryState || payload.delivery || status,
        deliveryReason: payload.deliveryReason || '',
      });
      if (!updated.questionAnswers?.length) _renderInputReceipt(updated);
      return payload;
    } catch (error) {
      if (!selectedCurrent()) throw error;
      const refused = error?.definiteInputPolicyRefusal === true;
      const retained = inputDelivery.saveReceipt(localStorage, { ...request, deliveryState: refused ? 'unavailable' : 'unknown', deliveryReason: error?.message || (refused ? 'Follow-up is unavailable' : 'Outcome unknown') });
      if (!retained.questionAnswers?.length) _renderInputReceipt(retained);
      throw error;
    }
  }

  // Follow-up to an EXISTING worker (O02): same durable O07 receipt + input endpoint as the composer, targeted at the
  // selected actor. No model options are sent, so the worker keeps its effective model; no new worker is created.
  export async function submitActorFollowUp({ actorID, actorSessionID, text, chat, mapRevision, executionRevision }) {
    const owner = _inputOwner;
    const chatID = sessionModule.getCurrentSessionId();
    const body = String(text || '').trim();
    if (!owner || !chatID || sessionModule.isCurrentSessionIncognito?.()) throw new Error('Durable follow-ups require a signed-in, saved chat.');
    if (!actorID || !actorSessionID || !body || chat !== chatID || !Number.isSafeInteger(mapRevision) || mapRevision < 0 || !Number.isSafeInteger(executionRevision) || executionRevision < 0) throw new Error('The selected worker or chat is unavailable.');
    const receipt = inputDelivery.saveReceipt(localStorage, {
      owner, chatID, requestID: inputDelivery.createRequestID(), text: body, uploadIDs: [], mode: 'after-turn',
      turnPolicy: _captureTurnPolicy(), actorID, actorSessionID, expectedMapRevision: mapRevision, expectedExecutionRevision: executionRevision, state: 'persisted', deliveryState: 'unknown',
    });
    return _submitDurableInput(receipt);
  }

  function _captureTurnPolicy() {
    const toggles = Storage.loadToggleState ? Storage.loadToggleState() : {};
    const mode = String(toggles?.chat_mode || 'agent');
    return {
      version: 1,
      mode: ['agent', 'chat', 'plan'].includes(mode) ? mode : 'agent',
      allowBash: !!uiModule.el('bash-toggle')?.checked,
      allowWebSearch: !!uiModule.el('web-toggle')?.checked,
      useWeb: false,
      useResearch: !!uiModule.el('research-toggle')?.checked,
      compareMode: false,
      incognito: !!uiModule.el('incognito-toggle')?.checked,
      noMemory: false,
    };
  }

  async function submitOptionalQuestionAnswer(question, answers, disposition = 'answered', deliberateRetry = false) {
    const owner = _inputOwner;
    const chatID = sessionModule.getCurrentSessionId();
    const requestID = String(question?.native_request_id || '');
    const questionRevision = Number(question?.question_revision);
    const actorID = String(question?.actor_id || '');
    const actorSessionID = String(question?.actor_session_id || '');
    const questionRef = String(question?.question_ref || '');
    if (!owner || !chatID || question?.session_id !== chatID || !requestID || !actorID || !actorSessionID || !questionRef
        || !Number.isSafeInteger(questionRevision) || questionRevision < 1
        || !['answered', 'dismissed'].includes(disposition)) {
      throw new Error('This question is missing its verified chat or actor binding.');
    }
    const answer = { requestID, questionRevision, disposition,
      ...(disposition === 'answered' ? { answers } : {}) };
    const ref = `${requestID}@${questionRevision}`;
    const turnPolicy = _captureTurnPolicy();
    const prior = inputDelivery.readReceipts(localStorage, owner, chatID)
      .find(item => item.questionRefs?.includes(ref) && item.actorID === actorID && item.actorSessionID === actorSessionID);
    if (prior) {
      if (JSON.stringify(prior.questionAnswers || []) !== JSON.stringify([answer])) {
        throw new Error('An answer for this question is already saved. Retry the saved answer or refresh its status.');
      }
      const retry = deliberateRetry
        ? inputDelivery.saveReceipt(localStorage, { ...prior, handoffID: inputDelivery.createRequestID(), deliveryState: 'unknown' })
        : prior;
      return _submitDurableInput(retry);
    }
    const receipt = inputDelivery.saveReceipt(localStorage, {
      owner, chatID, requestID: inputDelivery.createRequestID(), text: '', uploadIDs: [],
      questionRefs: [ref], questionAnswers: [answer], actorID, actorSessionID,
      turnPolicy,
      mode: isStreaming && !_inputQueueEnabled ? 'steer' : 'after-turn',
      state: 'persisted', deliveryState: 'unknown',
    });
    return _submitDurableInput(receipt);
  }

  async function _cancelDurableInput(receipt) {
    const scope = _durableInputScope();
    if (!scope || receipt.owner !== scope.account || receipt.chatID !== scope.chat) return;
    const chatID = scope.chat;
    _inputReceiptRead += 1;
    const overviewResponse = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}`, { credentials: 'same-origin' });
    if (!overviewResponse.ok) throw new Error('Could not verify this chat mapping for cancellation.');
    const overview = await overviewResponse.json();
    const mapRevision = Number(overview.mapRevision);
    if (!_durableInputScopeCurrent(scope)) return;
    if (!Number.isSafeInteger(mapRevision) || (receipt.expectedMapRevision !== undefined && mapRevision !== receipt.expectedMapRevision)) {
      throw new Error('The chat mapping changed before this follow-up could be withdrawn.');
    }
    const actorID = receipt.actorID || 'main';
    if (actorID !== 'main' && !receipt.actorSessionID) throw new Error('The original actor session is unavailable.');
    const query = new URLSearchParams({map_revision: String(mapRevision)});
    if (receipt.actorSessionID) query.set('actor_session_id', receipt.actorSessionID);
    const response = await fetch(`${API_BASE}/api/orchestration/${encodeURIComponent(chatID)}/actors/${encodeURIComponent(actorID)}/input/${encodeURIComponent(receipt.requestID)}?${query}`, {
      method: 'DELETE', credentials: 'same-origin',
    });
    const result = await response.json().catch(() => ({}));
    if (!_durableInputScopeCurrent(scope)) return;
    _inputReceiptRead += 1;
    if (!response.ok || !result.cancelled) throw new Error(result.detail?.message || 'Receipt could not be cancelled.');
    const updated = inputDelivery.saveReceipt(localStorage, { ...receipt, state: 'cancelled', deliveryState: 'cancelled' });
    _renderInputReceipt(updated);
  }

  async function _submitStreamingReceipt(mode = (_inputQueueEnabled ? 'after-turn' : 'steer')) {
    const input = uiModule.el('message');
    const text = String(input?.value || '').trim();
    const pendingFiles = fileHandlerModule.getPendingRaw?.() || [];
    const pendingCount = pendingFiles.length;
    if (!text && !pendingCount) return false;
    const scope = _durableInputScope();
    if (!scope) {
      uiModule.showError('Durable follow-ups require a signed-in, saved chat.');
      return true;
    }
    fileHandlerModule.invalidateFileAdditions();
    const { account: owner, chat: chatID } = scope;
    const requestID = inputDelivery.createRequestID();
    const draftVersion = _composerDraftVersion;
    const thinkingEpoch = _thinkingLoadEpoch;
    const turnPolicy = _captureTurnPolicy();
    let uploadIDs = [];
    let receiptSaved = false;
    try {
      // A Thinking change is persisted asynchronously. Freeze the draft scope
      // first, then wait for that exact save before freezing its option value.
      await _thinkingSavePromise;
      if (_thinkingSaveError) throw _thinkingSaveError;
      if (!_durableInputScopeCurrent(scope)
          || _composerDraftVersion !== draftVersion || String(input?.value || '').trim() !== text
          || _thinkingLoadEpoch !== thinkingEpoch) {
        throw new Error('The active account or chat changed before this follow-up was captured.');
      }
      const modelOptions = _thinkingSelection ? structuredClone(_thinkingSelection) : null;
      if (pendingCount) uploadIDs = await fileHandlerModule.uploadPending({ sessionId: chatID, files: pendingFiles });
      if (!_durableInputScopeCurrent(scope)) return true;
      if (pendingCount && uploadIDs.length !== pendingCount) {
        uiModule.showError('Attachment upload did not finish. Your draft and files are still available to retry.');
        return true;
      }
      const receipt = inputDelivery.saveReceipt(localStorage, {
        owner, chatID, requestID, text, uploadIDs, mode, turnPolicy,
        ...(modelOptions ? { modelOptions } : {}),
        state: 'persisted', deliveryState: 'unknown',
      });
      receiptSaved = true;
      await _submitDurableInput(receipt);
      if (input && _durableInputScopeCurrent(scope)
          && _composerDraftVersion === draftVersion && String(input.value || '').trim() === text) {
        input.value = '';
        input.style.height = '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return true;
    } catch (error) {
      if (!_durableInputScopeCurrent(scope)) return true;
      uiModule.showError(receiptSaved
        ? `Follow-up saved for retry: ${error?.message || error}`
        : `Message was not submitted: ${error?.message || error}`);
      return true;
    }
  }

  export function queueStreamingComposerRequest({forceAfterTurn = false} = {}) {
    if (!isStreaming) return false;
    const queuedInput = uiModule.el('message');
    const queuedText = (queuedInput && queuedInput.value || '').trim();
    if (!queuedText && !(fileHandlerModule.getPendingCount && fileHandlerModule.getPendingCount())) return false;
    void _submitStreamingReceipt(forceAfterTurn || _inputQueueEnabled ? 'after-turn' : 'steer');
    return true;
  }

  /**
   * Handle chat form submission
   */
  export async function handleChatSubmit(e, replacement = null, uiExplanation = false) {
    const appearanceOwner = _inputOwner, appearanceAuthEpoch = _codeEvidenceAuthEpoch;
    const appearanceNavigation = sessionModule.getSessionNavigationToken();
    let deliverableAppearance = null;
    try { deliverableAppearance = captureDeliverableAppearance(); } catch (_) { /* Theme owner not ready. */ }
    const attentionScope = captureContinuityScope(), attentionAuthEpoch = _codeEvidenceAuthEpoch;
    const attentionPendingChat = !!sessionModule.hasPendingChat?.();
    e.preventDefault();
    if (_replacementActive && (!replacement || replacement.reservation !== _replacementActive)) return;
    // A replacement is an exact, caller-owned attempt; never turn it into a
    // Stop, compare action, queued send, or a later ordinary composer send.
    if (replacement && (isStreaming || _sendInFlight || window.compareModule?.isActive() || !replacement.current())) return;
    // Cancel research clarification timeout if active
    if (window._researchTimeoutTimer) {
      clearTimeout(window._researchTimeoutTimer);
      window._researchTimeoutTimer = null;
    }
    // Get current session
    const sessionId = sessionModule.getCurrentSessionId();
    const session = sessionModule.getSessions().find(s => s.id === sessionId);
    
    const submitBtn = document.querySelector('.send-btn');
    
    // If compare is active, stop all compare streams
    if (window.compareModule && window.compareModule.isActive()) {
      window.compareModule.handleCompareSubmit();
      return;
    }

    // If currently streaming, keyboard Enter can queue a non-empty composer.
    // Clicking the stop icon should still stop normally, even if text exists.
    if (isStreaming) {
      const queueRequestedAt = Number(window.__odysseusQueueStreamingSubmit || 0);
      const shouldQueueStreamingSubmit = queueRequestedAt && Date.now() - queueRequestedAt < 1200;
      window.__odysseusQueueStreamingSubmit = 0;
      if (shouldQueueStreamingSubmit && queueStreamingComposerRequest()) {
        return;
      }
      if (fileHandlerModule.isUploading && fileHandlerModule.isUploading()) {
        fileHandlerModule.cancelUpload && fileHandlerModule.cancelUpload();
      }
      // Cancel server-side research if in progress
      const _cancelSid = sessionModule.getCurrentSessionId();
      if (_cancelSid && _researchingStreamIds.has(_cancelSid)) {
        fetch(`${API_BASE}/api/research/cancel/${_cancelSid}`, { method: 'POST' }).catch(e => console.warn('Research cancel failed:', e));
        _researchingStreamIds.delete(_cancelSid);
        _clearResearchTimer();
      }
      abortCurrentRequest(true);  // explicit user Stop → also cancel the detached server run

      // Clean up any running agent thread nodes (stop wave animation, remove "running" state)
      document.querySelectorAll('.agent-thread-node.running').forEach(node => {
        if (node._waveInterval) { clearInterval(node._waveInterval); node._waveInterval = null; }
        if (node._elapsedTicker) { clearInterval(node._elapsedTicker); node._elapsedTicker = null; }
        node.classList.remove('running');
        const wave = node.querySelector('.agent-thread-wave');
        if (wave) wave.textContent = '';
        const icon = node.querySelector('.agent-thread-icon');
        if (icon) icon.textContent = '\u25A0'; // stop square
        const statusEl = node.querySelector('.agent-thread-status');
        if (!statusEl) {
          const header = node.querySelector('.agent-thread-header');
          if (header) {
            const s = document.createElement('span');
            s.className = 'agent-thread-status';
            s.textContent = 'stopped';
            header.appendChild(s);
          }
        }
      });
      document.querySelectorAll('.agent-thread.streaming').forEach(t => t.classList.remove('streaming'));

      // Clean up any thinking spinners
      document.querySelectorAll('.agent-thinking-dots').forEach(el => {
        if (el._spinner) el._spinner.destroy();
        el.remove();
      });
      // No text accumulated — remove the empty holder with spinner
      if (currentHolder && !currentAccumulated) {
        if (currentSpinner) { currentSpinner.destroy(); currentSpinner = null; }
        // Empty cancel — keep the assistant bubble around with a "Cancelled
        // by user" indicator and persist a placeholder server-side so the
        // turn survives a refresh instead of vanishing without a trace.
        _renderCancelledBubble(currentHolder);
        currentHolder = null;
        updateSubmitButton('idle', submitBtn);
        const messageInput = uiModule.el('message');
        if (messageInput) messageInput.disabled = false;
        currentAccumulated = '';
        _refreshComposerQueue();
        return;
      }
      // Render whatever was accumulated so far
      if (currentHolder && currentAccumulated) {
        // Store accumulated in a closure variable before it gets cleared
        const stoppedContent = currentAccumulated;
        
        // Store raw content in dataset for consistency with other messages
        currentHolder.dataset.raw = stoppedContent;
        
        currentHolder.querySelector('.body').innerHTML = markdownModule.processWithThinking(
          markdownModule.squashOutsideCode(stoppedContent)
        );
        
        // Highlight code blocks
        if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(currentHolder);
        
        // Add the stopped indicator with continue button
        const stoppedIndicator = document.createElement('div');
        stoppedIndicator.className = 'stopped-indicator';
        const stoppedLabel = document.createElement('span');
        stoppedLabel.textContent = '[Message interrupted]';
        stoppedIndicator.appendChild(stoppedLabel);
        const continueBtn = document.createElement('button');
        continueBtn.className = 'continue-btn';
        continueBtn.title = 'Continue';
        continueBtn.textContent = '\u25B8';
        const _stoppedHolder = currentHolder; // capture before it gets cleared
        continueBtn.addEventListener('click', () => {
          stoppedIndicator.remove();
          _hideUserBubble = true;
          _pendingContinue = _stoppedHolder;
          const cutoff = stoppedContent;
          const msgInput = uiModule.el('message');
          if (msgInput) {
            msgInput.value = 'Your previous response was interrupted. It ended with:\n\n' + cutoff.slice(-500) + '\n\nDo NOT repeat what you already said. Continue exactly from where you were cut off.';
            const sb = document.querySelector('.send-btn');
            if (sb) sb.click();
          }
        });
        stoppedIndicator.appendChild(continueBtn);
        currentHolder.querySelector('.body').appendChild(stoppedIndicator);

        // Tell server to mark this message as stopped
        const _sid = sessionModule.getCurrentSessionId();
        if (_sid) fetch(`${API_BASE}/api/session/${_sid}/mark-stopped`, { method: 'POST' }).catch(e => console.warn('mark-stopped failed:', e));

        // Add footer with copy/regen if not already present
        if (!currentHolder.querySelector('.msg-footer')) {
          currentHolder.dataset.raw = stoppedContent;
          currentHolder.appendChild(createMsgFooter(currentHolder));
        }

        uiModule.scrollHistory();
      }
      
      // Reset button state
      updateSubmitButton('idle', submitBtn);
      
      // Re-enable message input
      const messageInput = uiModule.el('message');
      if (messageInput) messageInput.disabled = false;
      
      // Clear tracking variables
      currentAccumulated = '';
      currentHolder = null;

      return;
    }

    // --- Send-path entry: block re-clicks between submit and stream start ---
    if (_sendInFlight) return;
    const _sendPerf = _createChatSendPerf();
    _sendInFlight = true;
    if (!replacement) fileHandlerModule.invalidateFileAdditions();
    if (replacement) replacement.ownsSend = true;
    const currentThinkingScope = `${_inputOwner}\u0000${sessionModule.getCurrentSessionId() || ''}\u0000${!!uiModule.el('incognito-toggle')?.checked}`;
    if (_thinkingScopeKey !== currentThinkingScope) await _loadThinkingSelection(sessionModule.getCurrentSessionId());
    if (replacement && !replacement.current(true)) { replacement.ownsSend=false; _sendInFlight = false; _syncForegroundStreamGlobals(); return; }
    if (_inputOwner !== appearanceOwner || _codeEvidenceAuthEpoch !== appearanceAuthEpoch || sessionModule.getSessionNavigationToken() !== appearanceNavigation) {
      _sendInFlight = false; if (replacement) replacement.ownsSend = false; _syncForegroundStreamGlobals();
      uiModule.showError('The account or chat changed while preparing the message. The message was not sent.'); return;
    }
    const _sendCapture = {
      owner: _inputOwner,
      chatID: sessionModule.getCurrentSessionId(),
      navigationToken: sessionModule.getSessionNavigationToken(),
      draftVersion: _composerDraftVersion,
      draftText: String(uiModule.el('message')?.value || ''),
      thinkingEpoch: _thinkingLoadEpoch,
      optionRouteID: _thinkingOptionModel?.optionRouteId || '',
      privateMode: !!uiModule.el('incognito-toggle')?.checked,
      pendingFiles: replacement || uiExplanation ? [] : (fileHandlerModule.getPendingRaw?.() || []),
    };
    await _thinkingSavePromise;
    if (_thinkingSaveError || _inputOwner !== _sendCapture.owner
        || sessionModule.getCurrentSessionId() !== _sendCapture.chatID
        || sessionModule.getSessionNavigationToken() !== _sendCapture.navigationToken
        || _composerDraftVersion !== _sendCapture.draftVersion
        || String(uiModule.el('message')?.value || '') !== _sendCapture.draftText
        || _thinkingLoadEpoch !== _sendCapture.thinkingEpoch
        || !!uiModule.el('incognito-toggle')?.checked !== _sendCapture.privateMode
        || (_thinkingOptionModel?.optionRouteId || '') !== _sendCapture.optionRouteID
        || (replacement && !replacement.current(true))) {
      _sendInFlight = false;
      if (replacement) replacement.ownsSend = false;
      _syncForegroundStreamGlobals();
      uiModule.showError(_thinkingSaveError
        ? 'Thinking preference could not be saved. Retry the preference save before sending.'
        : 'The account, chat, route, or draft changed while waiting. The message was not sent.');
      return;
    }
    const _turnModelOptionsForSend = replacement?.options
      ? structuredClone(replacement.options)
      : (_thinkingSelection ? structuredClone(_thinkingSelection) : null);
    _setForegroundChatBusy(true);
    // Instant visual feedback so the user sees their click was accepted
    // even before the streaming button state kicks in below.
    const _earlyMessageInput = uiModule.el('message');
    if (_earlyMessageInput) _earlyMessageInput.disabled = true;
    if (submitBtn) submitBtn.classList.add('send-pending');
    const _releaseSendFlag = () => {
      _sendInFlight = false;
      if (replacement) replacement.ownsSend = false;
      _syncForegroundStreamGlobals();
      if (_earlyMessageInput) _earlyMessageInput.disabled = false;
      if (submitBtn) submitBtn.classList.remove('send-pending');
    };

    // --- Setup mode: intercept next message (but let slash commands through) ---
    if (!replacement && !uiExplanation) {
      const el = uiModule.el;
      const rawMsg = (el('message').value || '').trim();
      const currentSetupMode = slashCommands.getSetupMode();
      if (currentSetupMode && rawMsg && !isCommand(rawMsg)) {
        const mode = currentSetupMode;
        slashCommands.clearSetupMode(mode === 'endpoint-provider' || mode === 'endpoint-key-for-provider');
        el('message').value = '';
        if (window._syncModelPickerAutohide) window._syncModelPickerAutohide();
        if (uiModule.autoResize) uiModule.autoResize(el('message'));
        if (mode === true || mode === 'endpoint') {
          handleSetupInput(rawMsg);
        } else {
          handleSetupWizard(mode, rawMsg);
        }
        _releaseSendFlag();
        return;
      }
      if (currentSetupMode && rawMsg && isCommand(rawMsg)) {
        slashCommands.clearSetupMode();  // Clear setup mode, fall through to slash handler
      }
    }

    const el = uiModule.el;
    const msg = uiExplanation ? 'Buddy help · ' + uiExplanation : (replacement ? replacement.text : el('message').value);
    // Allow empty text when a regen carries over the original message's
    // attachment ids — a photo-only message still has something to send.
    if (!msg.trim() && !_sendCapture.pendingFiles.length && !(replacement ? replacement.attachments.length : (_pendingRegenAttachments && _pendingRegenAttachments.length))) { _releaseSendFlag(); return; }

    // --- Slash commands: execute directly without AI (no session needed) ---
    if (!replacement && isCommand(msg.trim())) {
      const slashSessionId = sessionModule.getCurrentSessionId();
      const handled = await handleSlashCommand(msg.trim());
      const commandInputStillCurrent = slashSessionId === sessionModule.getCurrentSessionId() && el('message').value === msg;
      if (handled) {
        if (commandInputStillCurrent) {
          el('message').value = '';
          if (window._syncModelPickerAutohide) window._syncModelPickerAutohide();
          if (uiModule.autoResize) uiModule.autoResize(el('message'));
        }
        _releaseSendFlag();
        return;
      }
      if (!commandInputStillCurrent) {
        uiModule.showToast('Chat or draft changed; the command was not sent.', 2400);
        _releaseSendFlag();
        return;
      }
    }

    const incognitoChkForSend = el('incognito-toggle');
    const isIncognitoForSend = !!(incognitoChkForSend && incognitoChkForSend.checked);

    if (!uiExplanation && !isIncognitoForSend && !sessionModule.getCurrentSessionId()) {
      const adoptionNav = sessionModule.getSessionNavigationToken();
      const adopted = await _adoptOpenedSessionBeforeAutoCreate();
      if (!adopted && sessionModule.getSessionNavigationToken() !== adoptionNav) {
        uiModule.showToast('Chat changed; the message was not sent.', 2400);
        _releaseSendFlag(); return;
      }
    }

    // A send belongs to this navigation, including while a picker PATCH waits.
    let sendNavToken = sessionModule.getSessionNavigationToken();
    const targetStillCurrent = () => sessionModule.getSessionNavigationToken() === sendNavToken;
    const cancelMovedSend = () => {
      uiModule.showToast('Chat changed; the message was not sent.', 2400);
      _releaseSendFlag();
    };
    try {
      // A second pick can replace the promise while the first one settles.
      while (window.__odysseusModelSwitchPromise) {
        await window.__odysseusModelSwitchPromise;
        if (!targetStillCurrent()) { cancelMovedSend(); return; }
      }
    } catch (_) {}
    if (!targetStillCurrent()) { cancelMovedSend(); return; }

    // Materialize pending session (deferred from model click) on first message
    if (sessionModule.hasPendingChat && sessionModule.hasPendingChat()) {
      _sendPerf.mark('pending_session_begin');
      const ok = await sessionModule.materializePendingSession();
      _sendPerf.mark('pending_session_done');
      if (!targetStillCurrent()) { cancelMovedSend(); return; }
      if (!ok || !sessionModule.getCurrentSessionId()) { _releaseSendFlag(); return; }
    }

    if (!sessionModule.getCurrentSessionId()) {
      // Auto-create a session using default chat config. Always fetch fresh
      // so that a recent Settings change takes effect without a page reload.
      try {
        const pending = sessionModule.getPendingChat && sessionModule.getPendingChat();
        if (pending && pending.url && pending.modelId) {
          const ok = await sessionModule.materializePendingSession();
          if (!targetStillCurrent()) { cancelMovedSend(); return; }
          if (!ok || !sessionModule.getCurrentSessionId()) { _releaseSendFlag(); return; }
        }
      } catch (_) {}
    }

    if (!sessionModule.getCurrentSessionId()) {
      // Auto-create a session using default chat config. Always fetch fresh
      // so that a recent Settings change takes effect without a page reload.
      try {
        let dc = (typeof window !== 'undefined' && window.__odysseusDefaultChat) || null;
        if (!dc || !dc.endpoint_url || !dc.model) {
          try {
            dc = JSON.parse(localStorage.getItem('openclank-default-chat-cache') || 'null');
          } catch (_) {}
        }
        try {
          if (!dc || !dc.endpoint_url || !dc.model) {
            _sendPerf.mark('default_chat_fetch_begin');
            const dcRes = await fetch('/api/default-chat');
            dc = await dcRes.json();
            _sendPerf.mark('default_chat_fetch_done');
            if (dc && dc.endpoint_url && dc.model) {
              try {
                window.__odysseusDefaultChat = dc;
                localStorage.setItem('openclank-default-chat-cache', JSON.stringify(dc));
              } catch (_) {}
            }
          }
        } catch (_) {
          dc = (typeof window !== 'undefined' && window.__odysseusDefaultChat) || null;
        }
        if (!targetStillCurrent()) { cancelMovedSend(); return; }
        if (dc && dc.endpoint_url && dc.model) {
          _sendPerf.mark('direct_chat_create_begin');
          sessionModule.createDirectChat(dc.endpoint_url, dc.model, dc.endpoint_id, 'default', { preserveComposer: true });
          sendNavToken = sessionModule.getSessionNavigationToken();
          _sendPerf.mark('direct_chat_create_done');
          const ok = await sessionModule.materializePendingSession();
          _sendPerf.mark('direct_chat_materialize_done');
          if (!targetStillCurrent()) { cancelMovedSend(); return; }
          if (!ok || !sessionModule.getCurrentSessionId()) { _releaseSendFlag(); return; }
        } else {
          uiModule.showError('Choose a provider connection and model before sending.');
          _releaseSendFlag(); return;
        }
      } catch (error) {
        if (targetStillCurrent()) uiModule.showError('Unable to start chat: ' + (error?.message || String(error)));
        _releaseSendFlag(); return;
      }
    }

    // --- API key guard: warn if message looks like an API key ---
    if (API_KEY_RE.test(msg.trim())) {
      if (!await window.styledConfirm('This looks like an API key. Sending it to the AI could expose it.\n\nDid you mean to use /setup instead?', { confirmText: 'Send anyway', danger: true })) {
        _releaseSendFlag();
        return;
      }
    }

    if (replacement && (!replacement.current(true) || !replacement.commit())) { _releaseSendFlag(); return; }
    if (replacement) { replacement.accepted = true; replacement.release(); }


    const messageInput = el('message');
    const originalBtnText = submitBtn ? submitBtn.innerHTML : '';

    // Re-enable the textarea now that we've handed off to the stream: the
    // user wants to compose the next message while the AI is still talking.
    // The `isStreaming` flag is the re-click guard for the send button.
    if (messageInput) messageInput.disabled = false;
    updateSubmitButton('streaming', submitBtn);
    if (submitBtn) submitBtn.classList.remove('send-pending');
    _sendInFlight = false;
    if (replacement) replacement.ownsSend = false;

    if (!targetStillCurrent()) { cancelMovedSend(); updateSubmitButton('idle', submitBtn); return; }
    const selectedRouteForSend = sessionModule.getCurrentRoute();

    // Capture session ID for background stream detection
    const streamSessionId = sessionModule.getCurrentSessionId();
    const dispatchAttentionScope = captureContinuityScope();
    const attentionStreamScope = targetStillCurrent() && streamSessionId &&
      _inputOwner === attentionScope.account && _sendCapture.owner === attentionScope.account &&
      _codeEvidenceAuthEpoch === attentionAuthEpoch && dispatchAttentionScope.account === attentionScope.account &&
      dispatchAttentionScope.authEpoch === attentionScope.authEpoch && dispatchAttentionScope.workspace === attentionScope.workspace &&
      (!attentionScope.chat || attentionScope.chat === streamSessionId || attentionPendingChat)
      ? { ...attentionScope, chat:streamSessionId } : null;
    _streamSessionId = streamSessionId;
    if (streamSessionId && _thinkingScopeKey !== `${_inputOwner}\u0000${streamSessionId}\u0000${!!uiModule.el('incognito-toggle')?.checked}`) {
      void _loadThinkingSelection(streamSessionId);
    }
    // A first send can materialize a host chat without a native actor map.
    // Load route-scoped preferences from the host Session before building its
    // turn envelope; native orchestration mapping is not part of this lookup.
    const streamQuery = msg;
    _touchStreamActivity(streamSessionId);

    // Acquire Web Lock to hint browser not to discard this tab while streaming
    if (navigator.locks) {
      navigator.locks.request('openclank-stream-' + streamSessionId, { mode: 'exclusive', ifAvailable: true }, lock => {
        if (!lock) return; // Another stream already holds a lock — fine
        return new Promise(resolve => { _webLockRelease = resolve; });
      }).catch(e => console.warn('web lock acquire failed:', e)); // Ignore lock errors — best-effort
    }

    // Declare accumulated outside try block so it's accessible in catch
    let accumulated = '';
    // Are we currently inside an unclosed <think> block? Toggled per think/answer
    // cycle so a multi-round agent response (one reasoning phase PER round) wraps each
    // round's reasoning in its own <think>…</think> instead of leaking rounds 2+ as text.
    let _thinkOpen = false;
    let holder = null;
    let finalMeta = null;
    let spinner = null;
    let dispatchStarted = false;
    let originalRunId = '';
    let timedOut = false;
    let admissionTimeout = null;
    let streamPhase = 'Preparing turn';
    const reportInterruptedRun = (message, runId = originalRunId) => {
      const scope = _durableInputScope();
      if (!targetStillCurrent() || !scope || scope.account !== _sendCapture.owner
          || _codeEvidenceAuthEpoch !== appearanceAuthEpoch || scope.workspace !== dispatchAttentionScope.workspace) return false;
      return _showOriginalStreamOutcome(holder, streamSessionId, runId, message);
    };
    let processingProbeTimer = null;
    let processingProbeAbort = null;
    let _renderStream = () => {};
    let _cancelThinkingTimer = () => {};
    let _removeThinkingSpinner = () => {};
    let abortCtrl = null;
    let sentUserBubble = null;
    let rejectedDraft = null;
    let postConsumeDraft = null;
    let admissionAccepted = false;
    const originalSendSelected = () => targetStillCurrent()
      && sessionModule.getCurrentSessionId() === streamSessionId && _inputOwner === _sendCapture.owner
      && _codeEvidenceAuthEpoch === appearanceAuthEpoch;
    const recoverRejectedSend = () => {
      if (!rejectedDraft || !postConsumeDraft || !originalSendSelected()) return;
      // A replacement never consumed the composer's independent draft.
      if (!replacement && !uiExplanation) restoreComposerDraft(rejectedDraft, { sid: streamSessionId, expected: postConsumeDraft });
      sentUserBubble?.remove();
      holder?.remove();
    };
    const _isAgent = true;
    let streamingTTS = false;
    let timeoutId = null;
    let responseTimeoutCleared = false;
    let clearResponseTimeout = () => {};
    let firstTokenWaitTimers = [];
    const clearFirstTokenWaitTimers = () => {
      firstTokenWaitTimers.forEach(t => { try { clearTimeout(t); } catch (_) {} });
      firstTokenWaitTimers = [];
    };
    const scheduleFirstTokenWaitMessages = () => {
      clearFirstTokenWaitTimers();
      const steps = [[20000, 'Waiting for first output'], [60000, 'Still waiting for first output'], [120000, 'No first output received yet']];
      firstTokenWaitTimers = steps.map(([ms, text]) => setTimeout(() => {
        if (!accumulated && spinner && spinner.element && !(abortCtrl && abortCtrl.signal.aborted)) {
          spinner.updateMessage(text);
        }
      }, ms));
    };
    const clearProcessingProbe = () => {
      if (processingProbeTimer) {
        clearTimeout(processingProbeTimer);
        processingProbeTimer = null;
      }
      if (processingProbeAbort) {
        try { processingProbeAbort.abort(); } catch (_) {}
        processingProbeAbort = null;
      }
    };

    // Reset tracking variables at start
    currentAccumulated = '';
    currentHolder = null;
    
    try {
      // Re-enable auto-scroll when user sends a message
      uiModule.setAutoScroll(true);
      uiModule.scrollHistoryInstant();
      // Clear completed dot now that user is interacting
      if (sessionModule.clearStreamComplete) sessionModule.clearStreamComplete(sessionModule.getCurrentSessionId());

      // Check for document selection context before consuming display override
      const docSel = !uiExplanation && documentModule && documentModule.getSelectionContext();
      if (docSel) {
        const sels = Array.isArray(docSel) ? docSel : [docSel];
        const lineRefs = sels.map(s =>
          s.startLine === s.endLine ? `L${s.startLine}` : `L${s.startLine}-${s.endLine}`
        );
        _displayOverride = `[Doc edit: ${lineRefs.join(', ')}] ${msg}`;
      }

      const userDisplay = _displayOverride || msg;
      _displayOverride = null;
      const skipBubble = replacement?.hideBubble || _hideUserBubble;
      if (!replacement) _hideUserBubble = false;
      // Auto-recovery counter: carries across a turn's auto-continues, but resets
      // when the user genuinely sends a new message (so each task gets a fresh cap).
      // A real user turn (visible bubble) ALWAYS resets the budget — even if a
      // prior auto-continue's deferred click never cleared the pending flag — so a
      // stuck flag can't silently eat the next turn's recovery budget.
      const _pendingAttachInfo = _sendCapture.pendingFiles.length ? fileHandlerModule.getPendingInfo(_sendCapture.pendingFiles) : null;
      // Pre-read importable file contents before upload clears pending files
      const IMPORTABLE_EXT = /\.(txt|py|js|ts|html|htm|css|md|json|csv|yml|yaml|sh|sql|rs|go|java|c|cpp|h|rb|php|xml|jsx|tsx|log|toml|ini|conf|env|vue|svelte|scss|sass|less)$/i;
      const _importableFiles = [];
      if (_pendingAttachInfo && documentModule) {
        const rawFiles = _sendCapture.pendingFiles;
        for (let i = 0; i < _pendingAttachInfo.length; i++) {
          const att = _pendingAttachInfo[i];
          if (IMPORTABLE_EXT.test(att.name) && rawFiles[i]) {
            _importableFiles.push({ info: att, file: rawFiles[i] });
          }
        }
      }
      let _userMsgEl = null;
      if (!skipBubble) {
        const userBubbleMetadata = {
          ...(_pendingAttachInfo ? { attachments: _pendingAttachInfo } : {}),
          ...(_turnModelOptionsForSend ? { model_request_options: _turnModelOptionsForSend } : {}),
          ...(uiExplanation ? { buddy_help: uiExplanation } : {}),
        };
        _userMsgEl = addMessage('user', userDisplay, null, userBubbleMetadata);
        sentUserBubble = _userMsgEl;
      }
      _sendPerf.mark('user_bubble_visible');
      if (!replacement && !uiExplanation) {
        messageInput.value = '';
        messageInput.style.height = '';
        messageInput.dispatchEvent(new Event('input'));
      }
      postConsumeDraft = captureComposerDraftState();
      // Mobile: dismiss the on-screen keyboard after sending. iOS in
      // particular ignores a bare blur() in some cases (or some other
      // listener refocuses straight after), so we temporarily mark the
      // input readonly which forces the keyboard to retract, then blur,
      // then drop the readonly attribute after the keyboard is gone so
      // typing still works for the next message.
      if (window.innerWidth <= 768) {
        try {
          messageInput.setAttribute('readonly', 'readonly');
          messageInput.blur();
          const _dropReadonly = () => { try { messageInput.removeAttribute('readonly'); } catch {} };
          setTimeout(() => {
            // If the blur stuck, the input is no longer the active element —
            // safe to drop readonly now so the next message can be typed.
            // If it did NOT stick (some mobile browsers keep the textarea
            // focused after a programmatic blur), removing readonly here would
            // re-summon the keyboard mid-stream — the "bounce up" that then
            // lingers until the end-of-stream blur. In that case keep readonly
            // on (keyboard stays down) and drop it the moment the user taps to
            // type again, so typing still works without the bounce.
            if (document.activeElement === messageInput) {
              messageInput.addEventListener('pointerdown', _dropReadonly, { once: true });
              messageInput.addEventListener('focus', _dropReadonly, { once: true });
            } else {
              _dropReadonly();
            }
          }, 120);
        } catch {}
      }

      let ids = [];
      try {
        _sendPerf.mark('upload_begin');
        ids = replacement ? [] : await fileHandlerModule.uploadPending({ sessionId: streamSessionId, files: _sendCapture.pendingFiles });
        _sendPerf.mark('upload_done');
      } catch(e) {
        console.error('upload failed', e);
        _sendPerf.mark('upload_failed');
      }
      if (_pendingAttachInfo && !ids.length && !(replacement ? replacement.attachments.length : (_pendingRegenAttachments && _pendingRegenAttachments.length))) {
        if (_userMsgEl && _userMsgEl.parentNode) _userMsgEl.remove();
        if (fileHandlerModule.wasLastUploadCancelled && !fileHandlerModule.wasLastUploadCancelled()) {
          uiModule.showError && uiModule.showError('Upload failed. Attachment kept so you can retry.');
        }
        updateSubmitButton('idle', submitBtn);
        _releaseSendFlag();
        return;
      }

      // Carry over the original message's file-ids on a regenerate so the new
      // send still references the same photos / docs (and picks up the user's
      // edited OCR text via the server-side .vision cache). Always CONSUME the
      // slot — even when empty / errored — so the regen ids can't bleed into
      // an unrelated next message if uploadPending() above had thrown.
      if (replacement) ids = ids.concat(replacement.attachments);
      else {
        if (!uiExplanation && _pendingRegenAttachments?.length) ids = ids.concat(_pendingRegenAttachments);
        _pendingRegenAttachments = null;
      }
      rejectedDraft = { available: true, content_kind: 'text', raw_content: msg,
        attachment_refs: ids.map(id => ({ type: 'attachment_ref', attachment_id: String(id) })) };
      if (!replacement) postConsumeDraft = { ...postConsumeDraft, attachments: null, pending: [] };

      // The optimistic user bubble was rendered before the upload assigned ids,
      // so image previews couldn't show (the renderer needs att.id). Now that
      // the upload resolved, stamp the ids — plus width/height for images so
      // the skeleton can size itself to the photo's aspect ratio — and
      // re-render so the thumbnail appears live, no refresh needed.
      if (_userMsgEl && _pendingAttachInfo && ids.length) {
        const _meta = fileHandlerModule.getLastUploadedMeta?.() || [];
        for (let i = 0; i < _pendingAttachInfo.length && i < ids.length; i++) {
          _pendingAttachInfo[i].id = ids[i];
          const _m = _meta[i];
          if (_m) {
            if (_m.width)  _pendingAttachInfo[i].width  = _m.width;
            if (_m.height) _pendingAttachInfo[i].height = _m.height;
          }
        }
        chatRenderer.updateMessageAttachments(_userMsgEl, _pendingAttachInfo);
      }

      // Offer to import text files to document library
      if (_importableFiles.length > 0) {
        const existing = document.getElementById('import-prompt-banner');
        if (existing) existing.remove();
        const banner = document.createElement('div');
        banner.id = 'import-prompt-banner';
        banner.className = 'import-prompt-banner';
        const label = _importableFiles.length === 1
          ? `Import "${_importableFiles[0].info.name}" to document library?`
          : `Import ${_importableFiles.length} files to document library?`;
        const textEl = document.createElement('span');
        textEl.textContent = label;
        banner.appendChild(textEl);
        const importBtn = document.createElement('button');
        importBtn.textContent = 'Import';
        importBtn.addEventListener('click', async () => {
          importBtn.disabled = true;
          importBtn.textContent = 'Importing…';
          const EXT_LANG = {'.py':'python','.js':'javascript','.ts':'typescript','.html':'html','.css':'css','.md':'markdown','.json':'json','.yml':'yaml','.yaml':'yaml','.sh':'bash','.sql':'sql','.rs':'rust','.go':'go','.java':'java','.c':'c','.cpp':'cpp','.rb':'ruby','.php':'php','.xml':'xml','.jsx':'javascript','.tsx':'typescript'};
          let imported = 0;
          for (const { info, file } of _importableFiles) {
            try {
              const content = await file.text();
              const dotIdx = info.name.lastIndexOf('.');
              const title = dotIdx > 0 ? info.name.slice(0, dotIdx) : info.name;
              const ext = dotIdx >= 0 ? info.name.slice(dotIdx).toLowerCase() : '';
              await fetch(`${API_BASE}/api/document`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, language: EXT_LANG[ext] || '', content }),
              });
              imported++;
            } catch (e) { console.error('Import failed:', info.name, e); }
          }
          banner.textContent = `Imported ${imported} file${imported !== 1 ? 's' : ''}`;
          setTimeout(() => banner.remove(), 2000);
        });
        banner.appendChild(importBtn);
        const dismissBtn = document.createElement('button');
        dismissBtn.textContent = '\u00d7';
        dismissBtn.className = 'import-prompt-dismiss';
        dismissBtn.setAttribute('aria-label', 'Dismiss');
        dismissBtn.title = 'Dismiss';
        dismissBtn.addEventListener('click', () => banner.remove());
        banner.appendChild(dismissBtn);
        const chatBar = document.querySelector('.chat-input-bar');
        if (chatBar) chatBar.parentNode.insertBefore(banner, chatBar);
        // Auto-dismiss after 15 seconds
        setTimeout(() => { if (banner.parentNode) banner.remove(); }, 15000);
      }

      // Auto-save document editor content before sending so the AI sees latest text
      const activeEmailComposerCtx = !uiExplanation && documentModule && typeof documentModule.getActiveEmailComposerContext === 'function'
        ? documentModule.getActiveEmailComposerContext()
        : null;
      let activeDocIdForSend = !uiExplanation && documentModule && typeof documentModule.getCurrentDocId === 'function'
        ? documentModule.getCurrentDocId()
        : null;
      if (activeEmailComposerCtx?.docId) {
        activeDocIdForSend = activeEmailComposerCtx.docId;
      }
      if (documentModule && activeDocIdForSend) {
        try {
          _sendPerf.mark('doc_save_begin');
          await documentModule.saveDocument();
          _sendPerf.mark('doc_save_done');
        } catch(e) {
          console.warn('doc auto-save failed', e);
          _sendPerf.mark('doc_save_failed');
        }
      }

      // Inject document selection context if present
      let finalMsg = msg;
      if (docSel) {
        const sels = Array.isArray(docSel) ? docSel : [docSel];
        if (sels.length === 1) {
          const s = sels[0];
          const lineRef = s.startLine === s.endLine ? `line ${s.startLine}` : `lines ${s.startLine}-${s.endLine}`;
          finalMsg = `In the document, edit this specific text (${lineRef}):\n\`\`\`\n${s.text}\n\`\`\`\n\nInstruction: ${msg}`;
        } else {
          const parts = sels.map((s, i) => {
            const lineRef = s.startLine === s.endLine ? `line ${s.startLine}` : `lines ${s.startLine}-${s.endLine}`;
            return `Selection ${i + 1} (${lineRef}):\n\`\`\`\n${s.text}\n\`\`\``;
          });
          finalMsg = `In the document, edit these specific sections:\n\n${parts.join('\n\n')}\n\nInstruction: ${msg}`;
        }
      }

      // Apply inject prefix/suffix
      const _inject = !uiExplanation && presetsModule.getInject ? presetsModule.getInject() : { prefix: '', suffix: '' };
      let _finalMsgWithInject = finalMsg;
      if (_inject.prefix) _finalMsgWithInject = _inject.prefix + ' ' + _finalMsgWithInject;
      if (_inject.suffix) _finalMsgWithInject = _finalMsgWithInject + ' ' + _inject.suffix;

      const fd = new FormData();
      fd.append('message', _finalMsgWithInject);
      if (uiExplanation) fd.append('buddy_help', uiExplanation);
      fd.append('session', streamSessionId);
      if (_inputOwner !== appearanceOwner || _codeEvidenceAuthEpoch !== appearanceAuthEpoch) throw new Error('The account changed while preparing the message.');
      if (deliverableAppearance) fd.append('deliverable_appearance', JSON.stringify(deliverableAppearance));
      if (_turnModelOptionsForSend) fd.append('model_options', JSON.stringify(_turnModelOptionsForSend));
      if (selectedRouteForSend.model) fd.append('selected_model', selectedRouteForSend.model);
      if (selectedRouteForSend.endpoint_url) fd.append('selected_endpoint_url', selectedRouteForSend.endpoint_url);
      if (selectedRouteForSend.endpoint_id) fd.append('selected_endpoint_id', selectedRouteForSend.endpoint_id);
      if (selectedRouteForSend.model_route_id) fd.append('selected_model_route_id', selectedRouteForSend.model_route_id);
      if (ids.length) fd.append('attachments', JSON.stringify(ids));
      // The contextual-help claim is held only through final preflight and
      // transport. Its release boundary below covers provider flushes and
      // every other step that can fail before response headers arrive.
      let helpClaim = null;
      let helpContext = null;
      let res;
      // Auto-save & send active doc ID so the backend sees latest content
      if (documentModule && activeDocIdForSend) {
        try {
          _sendPerf.mark('doc_silent_save_begin');
          await documentModule.saveDocument({ silent: true });
          _sendPerf.mark('doc_silent_save_done');
        } catch (_e) {
          _sendPerf.mark('doc_silent_save_failed');
        }
        fd.append('active_doc_id', activeDocIdForSend);
      }
      // Active email context — when an email reader is open, pass its
      // uid/folder/account so "reply", "summarize", "what does this say"
      // resolve to the email the user is actually looking at instead of
      // making the agent invent a new markdown draft with fake headers.
      try {
        const getEmailCtx = !uiExplanation && window.__odysseusGetActiveEmailContext;
        const emCtx = typeof getEmailCtx === 'function' ? getEmailCtx() : null;
        if (activeEmailComposerCtx && activeEmailComposerCtx.sourceUid) {
          fd.append('active_email_uid', String(activeEmailComposerCtx.sourceUid));
          fd.append('active_email_folder', String(activeEmailComposerCtx.sourceFolder || 'INBOX'));
        } else if (emCtx && emCtx.uid) {
          fd.append('active_email_uid', String(emCtx.uid));
          fd.append('active_email_folder', String(emCtx.folder || 'INBOX'));
          if (emCtx.account) fd.append('active_email_account', String(emCtx.account));
        }
      } catch (_e) { /* best-effort */ }
      const chatMode = uiExplanation ? 'chat' : (Storage.loadToggleState?.()?.chat_mode || 'agent');
      const incognitoChk = el('incognito-toggle');
      const isIncognito = !!(incognitoChk && incognitoChk.checked);
      fd.append('mode', chatMode);
      fd.append('allow_web_search', !uiExplanation && el('web-toggle').checked ? 'true' : 'false');
      if (!uiExplanation && el('research-toggle').checked) {
        fd.append('use_research', 'true');
      }
      fd.append('allow_bash', !uiExplanation && el('bash-toggle').checked ? 'true' : 'false');
      // Reserve the visible help attachment before the final Copal flush. The
      // complete pre-acceptance sequence is inside one release boundary, so a
      // flush, FormData, DOM, or transport failure cannot strand a claim.
      // These values also serve HTTP-error cleanup and the SSE loop after the
      // pre-acceptance boundary, so keep them in the complete send scope.
      const modelName = _bestKnownStreamModel(selectedRouteForSend) || null;
      const researchBtn = el('research-toggle-btn');
      const enableResearchBtn = () => {
        if (!researchBtn) return;
        researchBtn.disabled = false;
        researchBtn.classList.toggle('active', el('research-toggle').checked);
      };
      helpClaim = uiExplanation ? null : claimAssistantContext();
      helpContext = helpClaim?.context || null;
      try {
        // Copal contributes a bounded pointer. Flush committed Notes drafts
        // before sending so the server can rehydrate the authoritative Redb
        // resource; no body/owner/path crosses this boundary.
        try {
          const flushCopal = !uiExplanation && window.__odysseusFlushActiveCopalResource;
          if (typeof flushCopal === 'function') await flushCopal();
          const getCopal = !uiExplanation && window.__odysseusGetActiveCopalContext;
          const copalContext = typeof getCopal === 'function' ? getCopal() : null;
          if (copalContext && typeof copalContext === 'object') {
            // The shared getter also carries account scope for contextual Help.
            // Chat accepts only the four resource identifiers; owner scope is
            // derived from the authenticated session on the server.
            fd.append('active_copal_context', JSON.stringify({
              workspace: copalContext.workspace,
              view: copalContext.view ?? null,
              resourceKind: copalContext.resourceKind ?? null,
              resourceId: copalContext.resourceId ?? null,
            }));
          }
        } catch (error) {
          console.warn('Copal active-resource flush failed', error);
          throw error;
        }
        const ragChk = el('rag-toggle');
        if (uiExplanation || (ragChk && !ragChk.checked)) {
          fd.append('use_rag', 'false');
        }
        if (isIncognito) {
          fd.append('incognito', 'true');
        }
        const _workspaceId = (Storage.KEYS && Storage.get(Storage.KEYS.WORKSPACE_ID, '')) || '';
        // Always assert the stable per-chat binding, including explicit empty.
        // Raw paths remain a server-only compatibility lane for old clients.
        fd.append('workspace_id', _workspaceId);
        const activityWorkspace = sessionModule.getSessions().find(row=>row.id===streamSessionId)?.workspace_id;
        if (!uiExplanation && !isIncognito && captureCodeActivityPreference({accountID:_sendCapture.owner, workspaceID:activityWorkspace}))
          fd.append('observe_code_activity', 'true');
        if (!uiExplanation && presetsModule.getSelectedPreset()) {
          fd.append('preset_id', presetsModule.getSelectedPreset());
        }


        abortCtrl = new AbortController();
        abortCtrl._reason = '';
        currentAbort = abortCtrl;

        // Timeout: 6 min for research and agent mode, 3 min otherwise
        const timeoutMs = el('research-toggle').checked || _isAgent ? RESEARCH_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
        timeoutId = setTimeout(() => {
          if (!abortCtrl.signal.aborted) {
            timedOut = true;
            abortCtrl._reason = 'timeout';
            abortCtrl.abort();
          }
        }, timeoutMs);
        clearResponseTimeout = () => {
          if (responseTimeoutCleared) return;
          responseTimeoutCleared = true;
          clearTimeout(timeoutId);
        };
      
        const box = el('chat-history');
        holder = document.createElement('div');
        holder.className = 'msg msg-ai streaming';

        // Track holder globally so stop button can access it
        currentHolder = holder;
        _activeStreams.set(streamSessionId, {
          abortCtrl,
          holder,
          query: streamQuery,
          startedAt: Date.now(),
          lastActivity: Date.now(),
          runId: '',
        });
        _syncForegroundStreamGlobals();
        holder._researchQuery = msg; // Store query for notification text
      

        let loadingText = 'Initializing...';

        if (el('web-toggle').checked && !_isAgent) {
        const _searchLabel = searchModule ? searchModule.getProviderLabel() : 'web';
        loadingText = `Searching via ${_searchLabel}...<br>
                       <span style="font-size: 0.9em; opacity: 0.8;">
                       Query: "${msg.substring(0, 50)}${msg.length > 50 ? '...' : ''}"<br>
                       Fetching top results...</span>`;
        } else if (!uiExplanation && el('research-toggle').checked) {
        loadingText = 'Deep research mode active...';
        } else {
        loadingText = 'Processing request...';
        }

        var roleLabel = _modelRouteLabel(modelName, modelName);
        var _charNameInit = presetsModule.getCharacterName ? presetsModule.getCharacterName() : '';
        if (_charNameInit) roleLabel = _charNameInit;
        const roleTs = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
        holder.innerHTML = `<div class="role">${uiModule.esc(roleLabel)} <span class="role-timestamp">${roleTs}</span></div><div class="body"></div>`;
        holder._requestedModel = modelName;
        holder._actualModel = modelName;
        _applyModelColor(holder.querySelector('.role'), modelName);
        holder.style.position = 'relative';
      
        // Create spinner
        spinner = spinnerModule.create('Initializing', 'right', 'wave');
        currentSpinner = spinner;
        const bodyDiv = holder.querySelector('.body');
        bodyDiv.appendChild(spinner.createElement());
        spinner.start();
      
        // Update spinner message based on mode
        if (el('web-toggle').checked && !_isAgent) {
        spinner.updateMessage('Searching web with ' + (searchModule ? searchModule.getProviderLabel() : 'SearXNG'));
        setTimeout(() => spinner.updateMessage('Processing results'), 1500);
        } else if (!uiExplanation && el('research-toggle').checked) {
        spinner.updateMessage('Researching');
        setTimeout(() => spinner.updateMessage('Analyzing sources'), 1500);
        } else {
        spinner.updateMessage('Preparing turn');
        }
      
        if (!uiExplanation && el('research-toggle').checked && researchBtn) {
        researchBtn.disabled = true;
        researchBtn.classList.remove('active');
        }
        box.appendChild(holder);
        uiModule.scrollHistory();


        if (!uiExplanation && el('research-toggle').checked && researchBtn) {
        researchBtn.style.display = 'none';
        // Uncheck research toggle so follow-up messages don't trigger another research
        el('research-toggle').checked = false;
        }

        // User's current UTC offset in minutes (east of UTC). Threaded into
      // the agent so natural-language times like "today at 9pm" are
      // interpreted in YOUR timezone, not the server's.
        const _tzOffsetMin = -new Date().getTimezoneOffset();
        const _tzName = (() => {
        try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
        catch { return ''; }
        })();
        try {
        if (helpContext) fd.append('active_help_context', JSON.stringify({
          workspace: helpContext.workspace,
          surface: helpContext.surface,
          view: helpContext.view,
          resourceKind: helpContext.resourceKind,
          resourceId: helpContext.resourceId,
          resourceRef: helpContext.resourceRef,
          pinnedResourceId: helpContext.pinnedResourceId,
          pinnedResourceRef: helpContext.pinnedResourceRef,
          pinned: helpContext.pinned === true,
          selection: helpContext.selection,
          baseId: helpContext.baseId,
          baseQuery: helpContext.baseQuery,
          taskId: helpContext.taskId,
          taskQuery: helpContext.taskQuery,
          courseId: helpContext.courseId,
          lessonId: helpContext.lessonId,
          lessonTitle: helpContext.lessonTitle,
        }));
        _sendPerf.mark('chat_stream_post_begin');
        dispatchStarted = true;
        streamPhase = 'Submitting turn to Host';
        spinner?.updateMessage(streamPhase);
        admissionTimeout = setTimeout(() => {
          if (!abortCtrl.signal.aborted) {
            abortCtrl._reason = 'admission-timeout';
            abortCtrl.abort();
          }
        }, 60_000);
        res = await fetch(`${API_BASE}/api/chat_stream`, {
          method: 'POST',
          body: fd,
          headers: { 'X-Tz-Offset': String(_tzOffsetMin), 'X-Tz-Name': _tzName },
          signal: abortCtrl.signal
        });
      } catch (error) {
        // A missing response leaves Host admission unknown. Keep the context
        // available for a deliberate future action.
        restoreAssistantContext(helpClaim);
        throw error;
      }
      } catch (error) {
        // Any failure after claiming but before response headers releases the
        // reservation. A user who removed the chip or staged a newer context
        // remains authoritative because restore checks the claim token.
        restoreAssistantContext(helpClaim);
        throw error;
      }
      clearTimeout(admissionTimeout);
      if (res.ok) {
        admissionAccepted = true;
        commitAssistantContext(helpClaim);
        streamPhase = 'Waiting for first output';
        spinner?.updateMessage(streamPhase);
        scheduleFirstTokenWaitMessages();
      }
      const runId = res.headers.get('X-Agent-Run-ID') || '';
      originalRunId = runId;
      if (runId) {
        const active = _activeStreams.get(streamSessionId);
        if (active) active.runId = runId;
        const background = _backgroundStreams.get(streamSessionId);
        if (background) background.runId = runId;
      }
      _sendPerf.mark('chat_stream_headers');
      _sendPerf.report('headers_received');
      
      if (!res.ok) {
        restoreAssistantContext(helpClaim);
        clearResponseTimeout();
        const payload = await res.json().catch(() => null);
        const detail = payload?.detail ?? payload?.error;
        const message = typeof detail === 'string' ? detail : detail?.message;
        const errText = typeof message === 'string' && message.trim() ? message
          : `Open Clank could not complete the send (HTTP ${res.status}).`;
        // Only these admission errors prove the Host rejected the turn
        // before context persistence. A status code alone cannot prove it.
        const rejectedBeforeSave = !runId && res.status < 500 && ['selection_invalid','agent_disabled'].includes(detail?.code);
        if (rejectedBeforeSave) {
          recoverRejectedSend();
          if (originalSendSelected()) {
            uiModule.showError(detail.code === 'selection_invalid'
              ? `${errText} Choose an available connection and model in the picker.` : errText);
          }
        } else if (originalSendSelected()) {
          const outcome = `${errText} The original turn’s saved outcome is unverified.`;
          if (!reportInterruptedRun(outcome)) uiModule.showError(outcome);
        }
        enableResearchBtn();
        return;
      }

      // The server may restore a route-bound preference when the browser has
      // no cached selection yet. Persist the accepted turn envelope on the
      // optimistic user bubble so immediate regenerate uses that exact choice.
      try {
        const acceptedOptions = res.headers.get('X-Model-Request-Options');
        if (acceptedOptions && sentUserBubble) {
          const parsed = JSON.parse(acceptedOptions);
          if (parsed && typeof parsed === 'object' && parsed.route_id && Number.isInteger(parsed.catalog_revision)
              && parsed.options && typeof parsed.options === 'object') {
            sentUserBubble.dataset.modelRequestOptions = JSON.stringify(parsed);
          }
        }
      } catch (_) {}

      // Mark the chat log busy while streaming so screen readers wait for the
      // settled response instead of announcing every token. Cleared in finally.
      const _chatLog = document.getElementById('chat-history');
      if (_chatLog) _chatLog.setAttribute('aria-busy', 'true');

      const reader = res.body.getReader();
      _sendPerf.mark('reader_ready');
      _sendPerf.report('reader_ready');
      const decoder = new TextDecoder();
      let buffer = '';
      let metrics = null;
      let isThinking = false;
      let thinkingStartTime = null;
      // Streaming TTS: synthesize sentence-by-sentence during streaming
      streamingTTS = !!(window.aiTTSManager && window.aiTTSManager.autoPlay && window.aiTTSManager.available);
      if (streamingTTS) window.aiTTSManager.streamingStart();
      // Multi-bubble agent tracking
      let roundHolder = holder;       // Current AI text bubble (changes per round)
      let roundText = '';             // Text accumulated for current round
      let currentToolBubble = null;   // Current tool execution bubble
      // Tool cards keyed by the stream's tool-call id. The single-slot
      // currentToolBubble only works for a strictly sequential loop; the
      // mimo (ACP) lane runs tool calls in parallel and interleaves text
      // (agent_step nulls the slot), so a completion must find ITS card by
      // id or every earlier card spins forever — wave + elapsed intervals
      // ticking on each one until the tab crawls.
      let toolNodesById = Object.create(null);
      let lastToolThread = null;      // Visible tool timeline for tool-only turns
      let roundFinalized = false;     // Whether current round's text is finalized
      let _sourcesHtml = '';          // Sources box HTML to prepend to body
      let _sourcesExpanded = false;   // Track if user expanded sources during stream
      let _sourcesData = null;        // Raw sources data for rebuilding
      let _sourcesType = '';          // 'web' or 'research'
      let _findingsData = null;      // Raw findings data for collapsible box
      const _generatedImagesForTurn = [];
      function _rememberGeneratedImage(data) {
        const imageUrl = data?.image_url || data?.url || '';
        if (!imageUrl) return;
        const imageKey = String(data.image_id || imageUrl);
        if (_generatedImagesForTurn.some(x => String(x.image_id || x.image_url || x.url) === imageKey || x.image_url === imageUrl || x.url === imageUrl)) return;
        _generatedImagesForTurn.push({ ...data, image_url: imageUrl, url: imageUrl });
      }
      // _keepResearchOn removed — clarification state now persisted server-side via DB mode
      function _metricsTargetForTurn() {
        const visibleRound = (roundHolder && roundHolder.style.display !== 'none') ? roundHolder : null;
        const visibleText = visibleRound ? (visibleRound.querySelector('.body')?.textContent || '').trim() : '';
        if (lastToolThread && lastToolThread.isConnected && (!visibleRound || !visibleText || visibleText === 'Done.')) {
          return lastToolThread;
        }
        return visibleRound || holder;
      }
      // Insert sources box as a stable DOM node that won't be replaced during streaming.
      // Returns the content container to use for innerHTML updates.
      function _ensureStreamLayout(body) {
        if (!body) return body;
        // Sources are deferred to final render — don't insert during streaming
        // Ensure a stable content div exists for text content
        var contentDiv = body.querySelector('.stream-content');
        if (!contentDiv) {
          contentDiv = document.createElement('div');
          contentDiv.className = 'stream-content';
          body.appendChild(contentDiv);
        }
        return contentDiv;
      }
      function _ensureVisibleRoundForDelta() {
        if (!roundHolder || roundHolder.style.display !== 'none') return;
        const box = document.getElementById('chat-history');
        if (!box) {
          roundHolder.style.display = '';
          return;
        }
        const newWrap = document.createElement('div');
        newWrap.className = 'msg msg-ai msg-continuation streaming';
        const newRole = document.createElement('div');
        newRole.className = 'role';
        const metaS = sessionModule.getSessions().find(s => s.id === streamSessionId);
        inheritModelRouteState(holder, roundHolder, newWrap, metaS?.model || modelName);
        const requested = newWrap._requestedModel;
        const actual = newWrap._actualModel;
        newRole.textContent = _modelRouteLabel(requested, actual) || '';
        _applyModelColor(newRole, actual);
        newWrap.appendChild(newRole);
        const newBody = document.createElement('div');
        newBody.className = 'body';
        newWrap.appendChild(newBody);
        box.appendChild(newWrap);
        if (lastToolThread && lastToolThread.isConnected) lastToolThread.classList.add('has-bottom');
        roundHolder = newWrap;
        roundText = '';
        roundFinalized = false;
      }
      const esc = uiModule.esc;
      // Remove thinking spinner helper
      _removeThinkingSpinner = () => {
        const el = document.querySelector('.agent-thinking-dots');
        if (el) {
          if (el._spinner) el._spinner.destroy();
          el.remove();
        }
      };

      // Tool-aware thinking spinner
      let _lastToolName = '';
      const _searchIcon = uiIcon("search", 14, {"style":"vertical-align:-2px;margin-right:4px"});
      const _toolLabels = {
        'web_search': 'Searching',
        'bash': 'Running',
        'python': 'Running',
        'read_document': 'Reading',
        'edit_file': 'Editing',
        'read_file': 'Reading',
        'write_file': 'Writing',
        'create_document': 'Writing',
        'edit_document': 'Editing',
        'update_document': 'Rewriting',
        'suggest_document': 'Reviewing',
        'list_files': 'Browsing',
        'image_gen': 'Generating',
        'generate_image': 'Generating',
        'manage_memory': 'Remembering',
        'save_memory': 'Remembering',
        'search_memory': 'Recalling',
        'manage_session': 'Organizing',
        'deep_research': 'Researching',
        'list_models': 'Browsing',
        'ui_control': 'Adjusting',
      };
      const _toolIcons = {
        'web_search': _searchIcon,
      };
      function _thinkingLabel() {
        if (!_lastToolName) {
          return 'Thinking';
        }
        // Check exact match first, then prefix match
        const lower = _lastToolName.toLowerCase();
        if (_toolLabels[lower]) return _toolLabels[lower];
        for (const [key, label] of Object.entries(_toolLabels)) {
          if (lower.includes(key) || key.includes(lower)) return label;
        }
        return 'Thinking';
      }

      function _showThinkingSpinner(label) {
        if (document.querySelector('.agent-thinking-dots')) return;
        const _thinkMsg = document.createElement('div');
        _thinkMsg.className = 'msg msg-ai agent-thinking-dots';
        const _thinkBody = document.createElement('div');
        _thinkBody.className = 'body';
        const _ts = spinnerModule.create(label || 'Thinking', 'right', 'wave');
        _thinkBody.appendChild(_ts.createElement());
        _ts.start(120);
        _thinkMsg._spinner = _ts;
        _thinkMsg.appendChild(_thinkBody);
        document.getElementById('chat-history').appendChild(_thinkMsg);
        uiModule.scrollHistory();
      }

      function _replaceThinkingSpinner(label) {
        _removeThinkingSpinner();
        _showThinkingSpinner(label);
      }

      // Auto-show thinking spinner after text stops streaming
      let _textPauseTimer = null;
      const _thinkingSpinnerThrottle = createLiveThinkingThrottle(() => {
        _textPauseTimer = null;
        if (!document.querySelector('.agent-thinking-dots') && isStreaming) {
          _showThinkingSpinner(_thinkingLabel());
        }
      }, {
        delay: 400,
        schedule(callback, ms) {
          _textPauseTimer = setTimeout(callback, ms);
          return _textPauseTimer;
        },
        cancel(timer) {
          clearTimeout(timer);
          if (_textPauseTimer === timer) _textPauseTimer = null;
        },
      });
      function _scheduleThinkingSpinner() {
        _thinkingSpinnerThrottle.update(null);
      }
      _cancelThinkingTimer = () => {
        _thinkingSpinnerThrottle.cancel();
      };

      // Document streaming state (text-fence detection)
      let _docFenceOpened = false;
      let _docFenceContentStart = -1;
      let _liveThinkSection = null;
      let _liveThinkContent = null;
      let _liveThinkInner = null;
      let _liveThinkHeader = null;
      let _liveThinkSpinnerSlot = null;
      let _liveThinkTimerEl = null;
      let _liveThinkTokenCount = 0;
      let _liveThinkToggle = null;
      let _liveThinkDomId = null;

      function _estimateThinkingTokens(text) {
        const clean = (text || '').trim();
        if (!clean) return 0;
        return Math.max(1, Math.ceil(clean.length / 4));
      }

      function _formatThinkStats(seconds, tokenCount) {
        const time = seconds ? seconds + 's' : '';
        const tokens = tokenCount ? tokenCount + ' tok' : '';
        return time && tokens ? time + ' · ' + tokens : (time || tokens);
      }

      function _replyAfterClosedThinking(text) {
        text = markdownModule.normalizeThinkingMarkup(text || '');
        const closeRe = /<\/(?:think(?:ing)?|thought)>|<channel\|>/gi;
        let match = null;
        let last = null;
        while ((match = closeRe.exec(text || '')) !== null) last = match;
        if (!last) return '';
        return (text || '').slice(last.index + last[0].length).trimStart();
      }

      // Direct render helper for streaming text
      _renderStream = () => {
        let dt = markdownModule.normalizeThinkingMarkup(_streamDisplayText(roundText));
        const bodyEl = roundHolder.querySelector('.body');
        const contentEl = _ensureStreamLayout(bodyEl);

        // If thinking was already collapsed in-place, only render the reply portion
        let liveReply = contentEl.querySelector('.live-reply-content');
        if (liveReply) {
          // Extract reply text — handle native <think> tags and non-tag patterns
          const closedThinkReply = _replyAfterClosedThinking(dt);
          const { thinkingBlocks, content: replyText } = closedThinkReply
            ? { thinkingBlocks: [''], content: closedThinkReply }
            : markdownModule.extractThinkingBlocks(dt);
          let replyTrimmed = '';
          if (thinkingBlocks.length) {
            replyTrimmed = (replyText || '').trim();
          } else {
            // Non-tag: check for garbled <think> (reasoning\n<think>reply)
            const _gm = dt.match(/^[\s\S]+?<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>\s*([\s\S]*?)(?:<\/(?:think(?:ing)?|thought)>)?\s*$/i);
            if (_gm && _gm[1].trim()) {
              replyTrimmed = _gm[1].trim();
            } else {
              // Pure non-tag: find reply boundary
              const _rPrefixes = markdownModule.startsWithReasoningPrefix;
              const _rpStarts = ['Hey', 'Hi ', 'Hi!', 'Hello', 'Sure', 'Yes', 'No ', 'No,', 'Yo', 'OK', 'Here', 'Absolutely', 'Of course', 'Great', 'Alright', 'Thanks', 'Welcome', 'Good ', "I'm happy", "I'd be"];
              const _rt = (replyText || '').trimStart();
              if (_rPrefixes(_rt)) {
                const _rLines = _rt.split('\n');
                for (let _ri = 1; _ri < _rLines.length; _ri++) {
                  const _rl = _rLines[_ri].trim();
                  if (!_rl) continue;
                  if (_rpStarts.some(rp => _rl.startsWith(rp))) { replyTrimmed = _rLines.slice(_ri).join('\n'); break; }
                }
                if (!replyTrimmed) {
                  for (const rp of _rpStarts) {
                    const rx = new RegExp('[.!?]\\s*(' + rp.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')');
                    const m = rx.exec(_rt);
                    if (m && m.index > 20) { replyTrimmed = _rt.slice(m.index + 1).trim(); break; }
                  }
                }
              }
            }
          }
          if (replyTrimmed) {
            const r = liveReply._streamRenderer ||
              (liveReply._streamRenderer = createStreamRenderer(liveReply, {
                render: (t) => markdownModule.mdToHtml(markdownModule.squashOutsideCode(t)),
              }));
            r.update(replyTrimmed);
          }
          // Reply empty or not — preserve thinking bar, don't fall through to full re-render
          uiModule.scrollHistory();
          return;
        }

        // If thinking is still streaming (unclosed <think>), show indicator instead of raw text
        if (markdownModule.hasUnclosedThinkTag && markdownModule.hasUnclosedThinkTag(dt)) {
          const thinkStart = dt.search(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>|<\|channel>thought/i);
          const thinkContent = dt.substring(Math.max(thinkStart, 0))
            .replace(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>|<\|channel>thought\s*\n?/i, '')
            .replace(/<channel\|>/gi, '')
            .trim();
          const lines = thinkContent.split('\n').length;
          // Don't show beforeThink text during streaming — it'll appear in the final render
          // This prevents the "split into two" duplication
          contentEl.innerHTML =
            '<div class="thinking-section"><div class="thinking-header"><div class="thinking-header-left">Thinking' +
            (lines > 1 ? ` (${lines} lines)` : '') + '</div></div></div>';
          // The stream renderer self-heals when it next sees this overwritten
          // container (streamingRenderer.js), so no explicit reset is needed here.
          uiModule.scrollHistory();
          return;
        }

        // Incremental streaming render: freeze finalized blocks, re-render only the
        // growing tail, and highlight each code block once on completion. This is
        // what keeps code-block hover buttons from flickering and avoids the O(N^2)
        // re-parse/re-highlight of the whole message on every token.
        // See streamingRenderer.js / streamingSegmenter.js.
        if (_docFenceOpened && !dt.trim()) {
          _showDocumentWritingStatus(contentEl);
          uiModule.scrollHistory();
          return;
        }
        const renderer = contentEl._streamRenderer ||
          (contentEl._streamRenderer = createStreamRenderer(contentEl, {
            render: (t) => markdownModule.processWithThinking(markdownModule.squashOutsideCode(t)),
          }));
        renderer.update(dt);
        uiModule.scrollHistory();
      };

      let _nextIsError = false;
      let _streamSawDone = false;
      let _streamTerminalError = null;
      let _firstVisibleOutputSeen = false;
      const markFirstVisibleOutput = () => {
        if (_firstVisibleOutputSeen) return;
        _firstVisibleOutputSeen = true;
        streamPhase = 'Receiving response';
        clearFirstTokenWaitTimers();
      };

      // The terminal SSE frame is authoritative. Detached subscribers can
      // keep the HTTP body open briefly after [DONE], so do not wait for EOF.
      streamReadLoop:
      while (true) {
        const { done, value } = await reader.read();
        _touchStreamActivity(streamSessionId);
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          // Log SSE event types (e.g. "event: error") for debugging
          if (line.startsWith('event: ')) {
            const evtType = line.slice(7).trim();
            if (evtType === 'error') _nextIsError = true;
            continue;
          }
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data && data !== '[DONE]') markFirstVisibleOutput();

            // (thinking spinner removal is handled in agent_step / tool_start / content handlers)

            // Background detection: are we on a different session?
            const _isBg = (sessionModule.getCurrentSessionId() !== streamSessionId);

            // On first transition to background, store state in map
            if (_isBg && !_backgroundStreams.has(streamSessionId)) {
              _backgroundStreams.set(streamSessionId, {
                status: 'running',
                accumulated: accumulated,
                sourcesHtml: _sourcesHtml,
                findingsData: null,
                abortCtrl,
                query: streamQuery,
                metrics: null,
                runId: (_activeStreams.get(streamSessionId) || {}).runId || '',
              });
              if (sessionModule && sessionModule.markStreaming) {
                sessionModule.markStreaming(streamSessionId);
              }
            }

            if (data === '[DONE]') {
              _streamSawDone = true;
              // Always update background map if entry exists (even if user switched back)
              var bgDone = _backgroundStreams.get(streamSessionId);
              if (bgDone && !_isBg) {
                _backgroundStreams.delete(streamSessionId);
              } else if (bgDone) {
                bgDone.status = 'completed';
                bgDone.accumulated = accumulated;
                if (_isBg) {
                  try {
                    _notifyStreamComplete(streamSessionId, streamQuery, attentionStreamScope);
                    _insertStreamDoneToast(streamSessionId, streamQuery, attentionStreamScope);
                  } catch (toastErr) {
                    console.warn('[bg-stream] Toast/notification error:', toastErr);
                  }
                }
                // CRITICAL: always mark stream complete for the sidebar dot
                try {
                  if (sessionModule && sessionModule.markStreamComplete) {
                    sessionModule.markStreamComplete(streamSessionId);
                  }
                } catch (dotErr) {
                  console.warn('[bg-stream] markStreamComplete error:', dotErr);
                }
                // Don't do foreground final render — the checkBackgroundStream poll
                // will detect 'completed' and reload history cleanly
                try { await reader.cancel(); } catch (_) {}
                break streamReadLoop;
              }
              // Force-close thinking if still open (model never output boundary)
              if (isThinking) {
                isThinking = false;
                cancelAnimationFrame(_thinkTimerRAF);
                var _elapsedDone = thinkingStartTime ? ((Date.now() - thinkingStartTime) / 1000).toFixed(1) : null;
                if (_elapsedDone) {
                  accumulated = accumulated.replace(/<think>/i, '<think time="' + _elapsedDone + '">');
                  roundText = roundText.replace(/<think>/i, '<think time="' + _elapsedDone + '">');
                }
                if (_liveThinkHeader) _liveThinkHeader.textContent = 'View thinking process';
                if (_liveThinkSpinnerSlot) _liveThinkSpinnerSlot.remove();
                if (_liveThinkTimerEl && _elapsedDone) {
                  _liveThinkTimerEl.textContent = _formatThinkStats(_elapsedDone, _liveThinkTokenCount);
                  _liveThinkTimerEl.style.marginLeft = 'auto';
                  _liveThinkTimerEl.style.marginRight = '5px';
                  var _hdrDone = _liveThinkTimerEl.closest('.thinking-header');
                  // Keep the chevron furthest right with the timer to its left
                  // (match the live + final-render layout) — insert before the
                  // toggle rather than appending (which would land after it).
                  if (_hdrDone) {
                    if (_liveThinkToggle && _liveThinkToggle.parentElement === _hdrDone)
                      _hdrDone.insertBefore(_liveThinkTimerEl, _liveThinkToggle);
                    else _hdrDone.appendChild(_liveThinkTimerEl);
                  }
                }
                // Assign stable IDs
                var _thinkIdDone = 'think-' + Date.now();
                var _liveHdrDone = _liveThinkSection && _liveThinkSection.querySelector('.thinking-header');
                if (_liveHdrDone) _liveHdrDone.dataset.thinkingId = _thinkIdDone;
                if (_liveThinkContent) _liveThinkContent.id = _thinkIdDone;
                if (_liveThinkToggle) _liveThinkToggle.id = _thinkIdDone + '-toggle';
                // Create live-reply container so final render preserves thinking bar
                var _streamElDone = _liveThinkSection ? _liveThinkSection.parentElement : roundHolder.querySelector('.stream-content');
                if (!_streamElDone) _streamElDone = roundHolder.querySelector('.body');
                if (_streamElDone && !_streamElDone.querySelector('.live-reply-content')) {
                  var _replyElDone = document.createElement('div');
                  _replyElDone.className = 'live-reply-content';
                  _streamElDone.appendChild(_replyElDone);
                }
              }
              // Normal foreground completion — metrics will be displayed in the final render block below
              try { await reader.cancel(); } catch (_) {}
              break streamReadLoop;
            }
            try {
              const json = JSON.parse(data);
              // Handle SSE error events (e.g. HTTP 404 from provider)
              if (_nextIsError || json.status >= 400) {
                _nextIsError = false;
                const terminalStreamError = createTerminalStreamError(json);
                const rawError = terminalStreamError.message;
                const actionText = {
                  repair_credentials: 'Repair the endpoint credentials in Settings → Model Management.',
                };
                const actions = Array.isArray(json.actions)
                  ? json.actions.map(action => actionText[action]).filter(Boolean)
                  : [];
                const errMsg = [json.text || rawError || `Error ${json.status || 'unknown'}`, ...actions]
                  .join(' ');
                _streamTerminalError = createTerminalStreamError(json, errMsg);
                console.error('Stream error:', errMsg);
                if (spinner && spinner.element) spinner.destroy();
                try { await reader.cancel(); } catch (_) {}
                break streamReadLoop;
              }
              if (json.delta || json.type === 'agent_prep' || json.type === 'generated_image' || json.type === 'tool_start' || json.type === 'tool_output' || json.type === 'tool_progress' || json.type === 'agent_step' || json.type === 'loop_breaker_triggered' || json.type === 'intent_nudge_exhausted' || json.type === 'doc_stream_open' || json.type === 'doc_stream_delta' || json.type === 'research_progress') {
                clearResponseTimeout();
                clearProcessingProbe();
                clearFirstTokenWaitTimers();
              }
              if (json.type === 'generated_image') {
                _rememberGeneratedImage(json);
                if (!_isBg) _appendGeneratedImageBubble(json);
                continue;
              }
              if (json.type === 'agent_prep') {
                if (!_isBg) {
                  _cancelThinkingTimer();
                  _replaceThinkingSpinner('Preparing agent');
                }
                continue;
              }
              if (json.delta) {
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                // Text arrived after tools — connect thread line to this bubble
                const _threadAbove = roundHolder?.previousElementSibling;
                if (_threadAbove && _threadAbove.classList.contains('agent-thread') && !_threadAbove.classList.contains('has-bottom')) {
                  _threadAbove.classList.add('has-bottom');
                }
                // VLLM reasoning tokens: wrap in <think> tags for the thinking UI.
                // Stateful open/close (not a whole-message substring check) so each round
                // of a multi-round agent response gets its own <think>…</think> — otherwise
                // only round 1 is wrapped and rounds 2+ reasoning leaks into the answer.
                let _delta = json.delta;
                if (json.thinking) {
                  if (!_thinkOpen) { _delta = '<think>' + _delta; _thinkOpen = true; }
                } else if (_thinkOpen) {
                  _delta = '</think>' + _delta; _thinkOpen = false;
	                }
	                const wasEmpty = !accumulated;
		                accumulated += _delta;
		                if (!_isBg) currentAccumulated = accumulated; // Foreground stop-state text
	                // First token arrived — switch stop button from processing to streaming
	                if (wasEmpty && submitBtn && !_isBg) {
	                  submitBtn.dataset.phase = 'receiving';
                }

                // Update background map if running in background
                if (_isBg) {
                  var bgEntry = _backgroundStreams.get(streamSessionId);
	                  if (bgEntry) bgEntry.accumulated = accumulated;
	                  continue; // Skip all DOM writes
	                }
	                _ensureVisibleRoundForDelta();
	                roundText += _delta;

	                // --- Text-fence doc streaming (for models that don't use native tool calls) ---
                if (!_docFenceOpened && documentModule && (roundText.includes('```create_document\n') || roundText.includes('```document\n') || roundText.includes('```documen\n'))) {
                  const fenceMarker = roundText.includes('```document\n') ? '```document\n' : (roundText.includes('```documen\n') ? '```documen\n' : '```create_document\n');
                  const fenceIdx = roundText.indexOf(fenceMarker);
                  const afterFence = roundText.slice(fenceIdx + fenceMarker.length);
                  const fenceLines = afterFence.split('\n');
                  if (fenceLines.length >= 1 && fenceLines[0].trim()) {
                    _docFenceOpened = true;
                    const title = fenceLines[0].trim();
                    // Keep in sync with backend _KNOWN_LANGS in src/tool_implementations.py
                    const knownLangs = ['python','py','javascript','js','typescript','ts','html','css','json','yaml','bash','sql','rust','go','java','c','cpp','markdown','text','plain','ruby','swift','kotlin','php','email','csv','xml','toml','ini'];
                    const isLang = fenceLines.length >= 2 && knownLangs.includes(fenceLines[1].trim().toLowerCase());
                    const lang = isLang ? fenceLines[1].trim() : '';
                    _docFenceContentStart = fenceIdx + fenceMarker.length + title.length + 1 + (isLang ? fenceLines[1].length + 1 : 0);
                    documentModule.streamDocOpen(title, lang);
                  }
                }
                if (_docFenceOpened && _docFenceContentStart > 0 && documentModule) {
                  let raw = roundText.slice(_docFenceContentStart);
                  const closeIdx = raw.indexOf('\n```');
                  if (closeIdx >= 0) raw = raw.slice(0, closeIdx);
                  documentModule.streamDocDelta(raw);
                }

                // Detect thinking-in-progress:
                // 1. Normal: <think>...no closing tag yet
                // 2. Malformed: <think></think>\n...text but no second </think> yet
                // 3. Qwen3.5: "Thinking Process:" without <think> tags
                const normalizedRoundText = markdownModule.normalizeThinkingMarkup(roundText);
                let hasUnclosedThink = markdownModule.hasUnclosedThinkTag(normalizedRoundText);
                // Detect non-tag thinking patterns: "Thinking:", "Thinking Process:", Gemma-style reasoning
                // These patterns don't use <think> tags, so we simulate unclosed thinking during streaming
                const _replyPrefixes = ['Hey', 'Hi ', 'Hi!', 'Hello', 'Sure', 'Yes', 'No ', 'No,', 'Yo', 'OK', 'Here', 'Absolutely', 'Of course', 'Great', 'Alright', 'Thanks', 'Welcome', 'Good ', "I'm happy", "I'd be"];
                if (!hasUnclosedThink && !/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>|<\|channel>thought/i.test(normalizedRoundText)) {
                  const _trimmedRT = normalizedRoundText.trimStart();
                  const _isReasoning = markdownModule.startsWithReasoningPrefix(_trimmedRT);
                  if (_isReasoning) {
                    // Check if we can see a reply boundary yet (newline then reply pattern)
                    const _lines = _trimmedRT.split('\n');
                    let _replyFound = false;
                    for (let li = 1; li < _lines.length; li++) {
                      const _l = _lines[li].trim();
                      if (!_l) continue;
                      if (_replyPrefixes.some(rp => _l.startsWith(rp))) {
                        _replyFound = true;
                        break;
                      }
                    }
                    if (!_replyFound) {
                      // Also check within-line: "reasoning text.Reply text"
                      const _inlineReply = _replyPrefixes.some(rp => {
                        const rx = new RegExp('[.!?]\\s*' + rp.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
                        const m = rx.exec(_trimmedRT);
                        return m && m.index > 20;
                      });
                      if (!_inlineReply) hasUnclosedThink = true;
                    }
                  }
                }
                if (!hasUnclosedThink && /^<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>\s*<\/(?:think(?:ing)?|thought)>/i.test(normalizedRoundText)) {
                  // Empty <think></think> — the model likely put thinking outside the tags
                  const afterEmpty = normalizedRoundText.replace(/^<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>\s*<\/(?:think(?:ing)?|thought)>/i, '').trim();
                  const closeTags = (afterEmpty.match(/<\/(?:think(?:ing)?|thought)>/gi) || []).length;
                  if (closeTags === 0 && afterEmpty.length > 0) {
                    hasUnclosedThink = true; // still waiting for real closing tag
                  }
                }
                // Detect false close: <think>short</think> where real thinking follows untagged
                // Only applies when there's a second </think> later (model leaked thinking outside tags)
                // Do NOT trigger if the text after </think> contains tool calls (that's real content)
                if (!hasUnclosedThink && isThinking) {
                  const _thinkMatch = normalizedRoundText.match(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>([\s\S]*?)<\/(?:think(?:ing)?|thought)>/i);
                  const _thinkLen = _thinkMatch ? _thinkMatch[1].trim().length : 0;
                  if (_thinkLen < 20) {
                    const _afterClose = normalizedRoundText.replace(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>([\s\S]*?)<\/(?:think(?:ing)?|thought)>/i, '').trim();
                    // Only keep waiting if there's trailing text that looks like thinking (not tool calls)
                    const _hasToolCall = /```(?:bash|python|web_search|read_file|write_file|create_document|edit_document|manage_|generate_image)/i.test(_afterClose);
                    const _hasOrphanClose = /<\/(?:think(?:ing)?|thought)>/i.test(_afterClose);
                    if (!_hasToolCall && (_hasOrphanClose || (Date.now() - thinkingStartTime) < 500)) {
                      hasUnclosedThink = true; // keep waiting for real </think>
                    }
                  }
                }

                if (hasUnclosedThink && !isThinking) {
                  isThinking = true;
                  thinkingStartTime = Date.now();
                  if (spinner && spinner.element) spinner.destroy();

                  // Create a live thinking box — starts expanded so content streams visibly
                  var thinkBody = roundHolder.querySelector('.body');
                  var thinkContent = _ensureStreamLayout(thinkBody);
                  thinkContent.style.minHeight = '';
                  _liveThinkDomId = 'live-think-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
                  thinkContent.innerHTML = `
                    <div class="thinking-section">
                      <div class="thinking-header" data-thinking-id="${_liveThinkDomId}">
                        <div class="thinking-header-left"><span class="live-think-header-text">Thinking\u2026</span></div>
                        <span class="live-think-spinner-slot" style="flex-shrink:0;margin-left:auto;"></span>
                        <span class="live-think-timer" style="font-size:11px;opacity:0.4;font-variant-numeric:tabular-nums;margin-left:6px;margin-right:5px;"></span>
                        <span class="thinking-toggle live-think-toggle expanded" id="${_liveThinkDomId}-toggle"></span>
                      </div>
                      <div class="thinking-content expanded" id="${_liveThinkDomId}">
                        <div class="thinking-content-inner live-think-inner"></div>
                      </div>
                    </div>`;
                  _liveThinkSection = thinkContent.querySelector('.thinking-section');
                  _liveThinkContent = thinkContent.querySelector('.thinking-content');
                  _liveThinkInner = thinkContent.querySelector('.live-think-inner');
                  _liveThinkHeader = thinkContent.querySelector('.live-think-header-text');
                  _liveThinkSpinnerSlot = thinkContent.querySelector('.live-think-spinner-slot');
                  _liveThinkTimerEl = thinkContent.querySelector('.live-think-timer');
                  _liveThinkToggle = thinkContent.querySelector('.live-think-toggle');
                  // Live timer
                  var _thinkTimerStart = Date.now();
                  var _thinkTimerRAF = 0;
                  function _tickThinkTimer() {
                    if (!_liveThinkTimerEl || !_liveThinkTimerEl.isConnected) return;
                    var s = ((Date.now() - _thinkTimerStart) / 1000).toFixed(1);
                    _liveThinkTimerEl.textContent = _formatThinkStats(s, _liveThinkTokenCount);
                    _thinkTimerRAF = requestAnimationFrame(_tickThinkTimer);
                  }
                  _thinkTimerRAF = requestAnimationFrame(_tickThinkTimer);
                  // Whirlpool spinner
                  if (_liveThinkSpinnerSlot) {
                    var _wp = spinnerModule.createWhirlpool(12);
                    _wp.element.style.margin = '0';
                    _wp.element.style.width = '12px';
                    _wp.element.style.height = '12px';
                    _wp.element.style.transform = 'translateY(-1px)'; // align the whirlpool with the header text
                    _liveThinkSpinnerSlot.appendChild(_wp.element);
                  }
                } else if (hasUnclosedThink && isThinking) {
                  if (_liveThinkInner) {
                    // Extract raw thinking text (strip known thinking wrappers and prefixes)
                    var thinkText = markdownModule.normalizeThinkingMarkup(_streamDisplayText(roundText))
                      .replace(/<\/?(?:think(?:ing)?|thought)(?:\s+[^>]*)?>/gi, '')
                      .replace(/<\|channel>thought\s*\n?/gi, '')
                      .replace(/<\|channel>response\s*\n?/gi, '')
                      .replace(/<channel\|>/gi, '');
                    thinkText = thinkText.replace(/^\s*Thinking(?:\s+Process)?:\s*/i, '');
                    _liveThinkTokenCount = _estimateThinkingTokens(thinkText);
                    _liveThinkInner.innerHTML = markdownModule.mdToHtml(thinkText);
                    if (_liveThinkTimerEl) {
                      var _elapsedLive = thinkingStartTime ? ((Date.now() - thinkingStartTime) / 1000).toFixed(1) : '';
                      _liveThinkTimerEl.textContent = _formatThinkStats(_elapsedLive, _liveThinkTokenCount);
                    }
                    // Keep thinking box scrolled to bottom, but let user scroll up
                    var _followThinking = true;
                    var thinkBox = _liveThinkInner.closest('.thinking-content');
                    if (thinkBox) {
                      var nearBottom = thinkBox.scrollHeight - thinkBox.clientHeight - thinkBox.scrollTop < 80;
                      if (nearBottom) thinkBox.scrollTop = thinkBox.scrollHeight;
                      _followThinking = nearBottom;
                    }
                  }
                  if (_followThinking) uiModule.scrollHistory();
                  continue;
                } else if (!hasUnclosedThink && isThinking) {
                  isThinking = false;
                  var _thinkTextLen = _liveThinkInner ? _liveThinkInner.textContent.trim().length : 0;

                  // If thinking was trivially short (< 20 chars), remove the section entirely
                  // Models sometimes emit <think>The</think> or similar noise
                  if (_thinkTextLen < 20 && _liveThinkSection) {
                    _liveThinkSection.remove();
                    _liveThinkSection = null;
                    _liveThinkContent = null;
                    _liveThinkInner = null;
                    _liveThinkHeader = null;
                    _liveThinkSpinnerSlot = null;
                    _liveThinkTimerEl = null;
                    _liveThinkTokenCount = 0;
                    _liveThinkToggle = null;
                    _liveThinkDomId = null;
                    // Fall through to normal streaming
                    if (spinner && spinner.element) spinner.destroy();
                    _renderStream();
                    _scheduleThinkingSpinner();
                    continue;
                  }

                  // Thinking ended — smooth transition: update header, pause, then collapse
                  // Stop live timer and spinner
                  cancelAnimationFrame(_thinkTimerRAF);
                  var elapsed = thinkingStartTime ? ((Date.now() - thinkingStartTime) / 1000).toFixed(1) : null;
                  // Embed thinking time in the <think> tag for persistence on reload
                  if (elapsed) {
                    accumulated = accumulated.replace(/<think>/i, '<think time="' + elapsed + '">');
                    roundText = roundText.replace(/<think>/i, '<think time="' + elapsed + '">');
                  }
                  if (_liveThinkHeader) _liveThinkHeader.textContent = 'View thinking process';
                  if (_liveThinkSpinnerSlot) _liveThinkSpinnerSlot.remove();
                  // Move timer to right side of header
                  if (_liveThinkTimerEl && elapsed) {
                    _liveThinkTimerEl.textContent = _formatThinkStats(elapsed, _liveThinkTokenCount);
                    _liveThinkTimerEl.style.marginLeft = 'auto';
                    _liveThinkTimerEl.style.marginRight = '5px';
                    var _hdrRow = _liveThinkTimerEl.closest('.thinking-header');
                    // Chevron furthest right, timer to its left — insert before
                    // the toggle (appending would put the timer after it).
                    if (_hdrRow) {
                      if (_liveThinkToggle && _liveThinkToggle.parentElement === _hdrRow)
                        _hdrRow.insertBefore(_liveThinkTimerEl, _liveThinkToggle);
                      else _hdrRow.appendChild(_liveThinkTimerEl);
                    }
                  }

                  // Assign stable IDs (for click-toggle handler in markdown.js)
                  var _thinkId = 'think-' + Date.now();
                  var _liveHdr = _liveThinkSection && _liveThinkSection.querySelector('.thinking-header');
                  if (_liveHdr) _liveHdr.dataset.thinkingId = _thinkId;
                  if (_liveThinkContent) _liveThinkContent.id = _thinkId;
                  if (_liveThinkToggle) _liveThinkToggle.id = _thinkId + '-toggle';

                  // Append a container for the reply text that follows thinking
                  var _streamEl = _liveThinkSection ? _liveThinkSection.parentElement : roundHolder.querySelector('.stream-content');
                  if (!_streamEl) _streamEl = roundHolder.querySelector('.body');
                  if (_streamEl) {
                    var _replyEl = document.createElement('div');
                    _replyEl.className = 'live-reply-content';
                    _streamEl.appendChild(_replyEl);
                  }

                  // Render any reply text that arrived with the closing </think> token
                  _renderStream();
                } else {
                  // Normal streaming
                  if (spinner && spinner.element) spinner.destroy();
                  _renderStream();
                  _scheduleThinkingSpinner();
                  // Feed streaming TTS with accumulated text
                  if (streamingTTS) window.aiTTSManager.streamingUpdate(roundText);
                }
              } else if (json.type === 'research_progress') {
                if (_isBg) continue; // Skip DOM updates in background
                _researchingStreamIds.add(streamSessionId);
                // Highlight research button while running
                var _rToggle = document.getElementById('research-toggle-btn');
                if (_rToggle) _rToggle.classList.add('research-running');
                // Request notification permission on first research event
                if ('Notification' in window && Notification.permission === 'default') {
                  Notification.requestPermission();
                }
                // Mark session as researching in sidebar
                var _rSid = sessionModule && sessionModule.getCurrentSessionId();
                if (_rSid && sessionModule.markResearching) sessionModule.markResearching(_rSid);
                const rp = json.data;
                // Start research timer + synapse on first progress event
                if (!_researchTimerEl && spinner && spinner.element) {
                  _researchStartTime = rp.started_at ? rp.started_at * 1000 : Date.now();
                  _researchAvgDuration = rp.avg_duration || null;
                  _researchTimerEl = document.createElement('div');
                  _researchTimerEl.className = 'research-timer';
                  // Styles in .research-timer CSS class
                  spinner.element.parentNode.insertBefore(_researchTimerEl, spinner.element.nextSibling);
                  _researchTimerInterval = setInterval(() => {
                    if (!_researchTimerEl) return;
                    var elapsed = Math.floor((Date.now() - _researchStartTime) / 1000);
                    var mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
                    var ss = String(elapsed % 60).padStart(2, '0');
                    var txt = mm + ':' + ss;
                    if (_researchAvgDuration) {
                      var avgM = String(Math.floor(_researchAvgDuration / 60)).padStart(2, '0');
                      var avgS = String(Math.round(_researchAvgDuration % 60)).padStart(2, '0');
                      txt += ' / avg ' + avgM + ':' + avgS;
                    }
                    _researchTimerEl.textContent = txt;
                  }, 1000);
                  // Synapse visualization — insert right above the timer so
                  // it sits between the spinner message and the timer line.
                  try {
                    _researchSynapse = createResearchSynapse(spinner.element.parentNode, {
                      query: holder._researchQuery || rp.query || '',
                      startedAt: _researchStartTime,
                    });
                    // Move it to live between spinner and timer
                    if (_researchSynapse.element && _researchTimerEl) {
                      spinner.element.parentNode.insertBefore(_researchSynapse.element, _researchTimerEl);
                    }
                  } catch (e) { console.warn('synapse init failed', e); }
                }
                if (_researchSynapse) {
                  _researchSynapse.setPhase(rp.phase, rp);
                  if (typeof rp.round === 'number') _researchSynapse.setRound(rp.round);
                  if (typeof rp.total_sources === 'number') _researchSynapse.setSourceCount(rp.total_sources);
                  if (rp.phase === 'error') _researchSynapse.complete();
                }
                if (spinner && spinner.element) {
                  if (rp.phase === 'probing') {
                    spinner.updateMessage(`Verifying model: ${rp.model || '?'}`);
                  } else if (rp.phase === 'planning') {
                    spinner.updateMessage('Analyzing question & planning research strategy');
                  } else if (rp.phase === 'searching') {
                    const q = rp.queries ? `${rp.queries} queries` : '';
                    const s = rp.total_sources ? ` · ${rp.total_sources} sources` : '';
                    spinner.updateMessage(`Round ${rp.round || '?'}: Searching${q ? ' (' + q + ')' : ''}${s}`);
                  } else if (rp.phase === 'reading') {
                    spinner.updateMessage(rp.title ? `Reading: ${rp.title}` : `Round ${rp.round || '?'}: Reading ${rp.new_sources || ''} pages · ${rp.total_sources || 0} sources total`);
                  } else if (rp.phase === 'analyzing') {
                    spinner.updateMessage(`Round ${rp.round || '?'}: Analyzing ${rp.total_findings || 0} findings`);
                  } else if (rp.phase === 'writing') {
                    spinner.updateMessage(`Writing report · ${rp.total_sources || 0} sources`);
                  } else if (rp.phase === 'error') {
                    spinner.updateMessage(rp.message || 'Search error');
                  }
                }
              } else if (json.type === 'research_sources') {
                if (_isBg) {
                  // Store sources HTML in background map
                  if (json.data && json.data.length > 0) {
                    _sourcesHtml = _buildSourcesBox(json.data, 'research');
                    var bgE = _backgroundStreams.get(streamSessionId);
                    if (bgE) bgE.sourcesHtml = _sourcesHtml;
                  }
                  // Clear researching indicator for this background session
                  if (sessionModule && sessionModule.clearResearching) sessionModule.clearResearching(streamSessionId);
                  continue;
                }
                // Research done — clean up timer, show sources box, then spinner for LLM response
                _clearResearchTimer();
                holder._researchSources = json.data;
                var _rSid2 = sessionModule && sessionModule.getCurrentSessionId();
                if (_rSid2 && sessionModule.clearResearching) sessionModule.clearResearching(_rSid2);
                if (json.data && json.data.length > 0) {
                  _sourcesData = json.data; _sourcesType = 'research';
                  _sourcesHtml = _buildSourcesBox(json.data, 'research');
                }
                if (document.hidden) {
                  _notifyResearchComplete(_rSid2 || '', holder._researchQuery || '', attentionStreamScope);
                }
              } else if (json.type === 'research_findings') {
                if (_isBg) {
                  var bgEf = _backgroundStreams.get(streamSessionId);
                  if (bgEf) bgEf.findingsData = json.data;
                  continue;
                }
                if (json.data && json.data.length > 0) {
                  _findingsData = json.data;
                }
              } else if (json.type === 'research_done') {
                // Research complete — reload session to show the persisted report
                _clearResearchTimer();
                if (sessionModule && sessionModule.clearResearching) {
                  sessionModule.clearResearching(streamSessionId);
                }
                _researchingStreamIds.delete(streamSessionId);
                // Small delay then reload session history which includes the full report
                setTimeout(async () => {
                  // Don't yank the user back to this chat if they've navigated
                  // away (e.g. started a new chat) while research finished —
                  // just refresh the sidebar so the report shows when they return.
                  if (sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId() === streamSessionId) {
                    await sessionModule.selectSession(streamSessionId);
                  } else {
                    await sessionModule.loadSessions();
                  }
                }, 500);
                continue;
              } else if (json.type === 'web_sources') {
                if (_isBg) {
                  if (json.data && json.data.length > 0) {
                    _sourcesHtml = _buildSourcesBox(json.data, 'web');
                    var bgE2 = _backgroundStreams.get(streamSessionId);
                    if (bgE2) bgE2.sourcesHtml = _sourcesHtml;
                  }
                  continue;
                }
                // Web search done — store sources for final render (don't render mid-stream)
                holder._webSources = json.data;
                if (json.data && json.data.length > 0) {
                  _sourcesData = json.data; _sourcesType = 'web';
                  _sourcesHtml = _buildSourcesBox(json.data, 'web');
                }
              } else if (json.type === 'workspace_rejected') {
                // The server has already invalidated this chat's binding. Keep
                // the local session projection in sync, but never PATCH it back
                // (which would create a rejection loop). A background chat must
                // also never clear the Workspace currently painted for another
                // foreground chat.
                sessionModule.setSessionWorkspaceId?.(streamSessionId, '');
                if (sessionModule.getCurrentSessionId?.() === streamSessionId) {
                  import('./workspace.js').then(async (m) => {
                    const ws = m.default || m;
                    if (ws && ws.setWorkspace) {
                      await ws.setWorkspace('', '', { persist: false });
                    }
                  }).catch(() => {});
                }
                uiModule.showToast(
                  'This chat’s Workspace is no longer usable; running without confinement',
                  6000
                );
                continue;
              } else if (json.type === 'model_fallback') {
                // Model went offline — switched to fallback
                var _fbData = json.data || {};
                uiModule.showToast(
                  `Model ${_fbData.old_model || '?'} offline — switched to ${_fbData.new_model || '?'}`,
                  5000
                );
                // Update the model picker to reflect the new model
                if (sessionModule && sessionModule.updateModelPicker) {
                  sessionModule.updateModelPicker();
                }
                continue;
              } else if (json.type === 'model_info') {
                // Update role label with model name as soon as we know it
                if (!_isBg && holder) {
                  const roleEl = holder.querySelector('.role');
                  if (roleEl) {
                    applyModelRouteEventState(json, holder, roundHolder, modelName, _sameModelName);
                    if (json.suffix) holder._roleSuffix = json.suffix;
                    // Prepend character name if sent by server or set locally
                    var _charName = json.character_name || (presetsModule.getCharacterName ? presetsModule.getCharacterName() : '');
                    if (_charName) holder._characterName = _charName;
                    _setRoleModelLabel(roleEl, holder._requestedModel, holder._actualModel, {
                      suffix: holder._roleSuffix,
                      characterName: holder._characterName,
                    });
                  }
                }
              } else if (json.type === 'fallback') {
                // The selected model failed and another provider answered. Make
                // it visible so a misconfigured provider is never silently
                // masked under the selected model's name.
                if (!_isBg) {
                  var _selM = _shortModel(json.selected_model || '');
                  var _ansM = _shortModel(json.answered_by || '');
                  uiModule.showToast('⚠ ' + _selM + ' failed — answered by ' + _ansM, 6000);
                  var _fallbackHolder = applyModelRouteEventState(json, holder, roundHolder, modelName, _sameModelName);
                  if (_fallbackHolder) {
                    var _rEl = _fallbackHolder.querySelector('.role');
                    if (_rEl) {
                      var _tsS = _rEl.querySelector('.role-timestamp');
                      _rEl.textContent = _ansM + ' (fallback) ';
                      _rEl.title = (json.selected_model || '') + ' failed' +
                        (json.reason ? ': ' + json.reason : '') + ' — answered by ' + (json.answered_by || '');
                      _applyModelColor(_rEl, json.answered_by);
                      if (_tsS) _rEl.appendChild(_tsS);
                      _setRoleModelLabel(_rEl, _fallbackHolder._requestedModel, _fallbackHolder._actualModel, {
                        suffix: _fallbackHolder._roleSuffix,
                        characterName: _fallbackHolder._characterName,
                        reason: json.reason,
                      });
                    }
                  }
                }
              } else if (json.type === 'rounds_exhausted') {
                // The agent hit the per-turn step limit while still working.
                // Offer a Continue button instead of stalling silently.
                // NOTE: append to the chat-history container (bottom), NOT the
                // message body — the body innerHTML is re-rendered at stream
                // finalize, which would wipe a note placed inside it.
                const _chatBox = document.getElementById('chat-history');
                if (!_isBg && _chatBox) {
                  // Drop any prior box so repeated cap-hits each get a fresh
                  // Continue at the bottom (multiple continues in a row).
                  const _old = _chatBox.querySelector('.rounds-exhausted');
                  if (_old) _old.remove();
                  const note = document.createElement('div');
                  note.className = 'stopped-indicator rounds-exhausted';
                  const label = document.createElement('span');
                  label.className = 'rounds-exhausted-label';
                  label.textContent = `Reached the ${json.rounds || ''}-step limit — not finished.`;
                  note.appendChild(label);
                  const contBtn = document.createElement('button');
                  contBtn.className = 'continue-btn';
                  contBtn.title = 'Continue the task';
                  contBtn.textContent = 'Continue ▸';
                  const _holder = holder;
                  contBtn.addEventListener('click', () => {
                    note.remove();
                    _hideUserBubble = true;
                    _pendingContinue = _holder;
                    const msgInput = uiModule.el('message');
                    if (msgInput) {
                      msgInput.value = 'You hit the step limit before finishing — the task is not complete. Continue from exactly where you left off and keep going until it is done. Do NOT repeat work already done.';
                      const sb = document.querySelector('.send-btn');
                      if (sb) sb.click();
                    }
                  });
                  note.appendChild(contBtn);
                  _chatBox.appendChild(note);
                  try { note.scrollIntoView({ block: 'end', behavior: 'smooth' }); } catch (_) { uiModule.scrollHistory && uiModule.scrollHistory(); }
                }
              } else if (json.type === 'model_actual') {
                if (!_isBg) {
                  var _modelHolder = applyModelRouteEventState(json, holder, roundHolder, modelName, _sameModelName);
                  if (_modelHolder) _setRoleModelLabel(_modelHolder.querySelector('.role'), _modelHolder._requestedModel, _modelHolder._actualModel, {
                    suffix: _modelHolder._roleSuffix,
                    characterName: _modelHolder._characterName,
                  });
                }
              } else if (json.type === 'attachments') {
                if (_isBg) continue;
                // Update user bubble — replace file chips with image previews
                const _ub = document.querySelector('#chat-history .msg-user:last-of-type');
                if (_ub) {
                  const _aw = _ub.querySelector('.attach-cards');
                  if (_aw) {
                    for (const _att of json.data) {
                      const _isImg = (_att.mime || '').startsWith('image/') || /\.(png|jpg|jpeg|gif|webp|svg|bmp)$/i.test(_att.name || '');
                      if (_isImg && _att.id) {
                        // Skip if we already have a preview for this file id —
                        // on a regenerate the original user bubble keeps its
                        // photo and the backend re-emits the attachment event
                        // for the same id; without this guard we'd append a
                        // duplicate (which visually pushes the real photo off).
                        const _existingPreview = _aw.querySelector('[data-file-id="' + _att.id + '"]');
                        if (_existingPreview) {
                          if (_att.vision_model && !_existingPreview.querySelector('.attach-vision-model')) {
                            const _vl = document.createElement('div');
                            _vl.className = 'attach-vision-model';
                            _vl.textContent = 'Vision: ' + String(_att.vision_model).split('/').pop();
                            const _name = _existingPreview.querySelector('.attach-image-name');
                            if (_name) _existingPreview.insertBefore(_vl, _name);
                            else _existingPreview.appendChild(_vl);
                          }
                          continue;
                        }
                        const _card = _aw.querySelector('.attach-card[data-name="' + (_att.name || '').replace(/"/g, '\\"') + '"]');
                        const _iw = document.createElement('div');
                        _iw.className = 'attach-image-preview';
                        _iw.dataset.fileId = _att.id;
                        _iw.style.cursor = 'pointer';
                        _iw.onclick = () => window.open(API_BASE + '/api/upload/' + _att.id, '_blank');
                        const _im = document.createElement('img');
                        _im.src = API_BASE + '/api/upload/' + _att.id;
                        _im.alt = _att.name || 'Image';
                        _im.style.cssText = 'max-width:300px;max-height:200px;border-radius:6px;display:block;';
                        _iw.appendChild(_im);
                        if (_att.vision_model) {
                          const _vl = document.createElement('div');
                          _vl.className = 'attach-vision-model';
                          _vl.textContent = 'Vision: ' + String(_att.vision_model).split('/').pop();
                          _iw.appendChild(_vl);
                        }
                        if (_att.name) {
                          const _nm = document.createElement('div');
                          _nm.className = 'attach-image-name';
                          _nm.textContent = _att.name;
                          _iw.appendChild(_nm);
                        }
                        if (_card) _card.replaceWith(_iw); else _aw.appendChild(_iw);
                      } else {
                        const _card = _aw.querySelector('.attach-card[data-name="' + (_att.name || '').replace(/"/g, '\\"') + '"]');
                        if (_card && _att.id) {
                          _card.dataset.fileId = _att.id;
                          _card.style.cursor = 'pointer';
                          _card.onclick = () => window.open(API_BASE + '/api/upload/' + _att.id, '_blank');
                        }
                      }
                    }
                  }
                  // Caption / OCR text is no longer rendered as an inline
                  // collapsible on the user bubble — the user can view/edit
                  // it via the "Caption" button on the photo thumbnail.
                }
              } else if (json.type === 'rag_sources') {
                if (_isBg) continue;
                holder._ragSources = json.data;
              } else if (json.type === 'memories_used') {
                if (_isBg) continue;
                holder._memoriesUsed = json.data;
              } else if (json.type === 'compacted') {
                if (!_isBg && runId) reportCodeContextBoundary(chatCodeBoundaryScope(streamSessionId,_sendCapture.owner,_sendCapture.privateMode),{
                  operation:'compact',eventID:'auto:'+runId,observedAt:json.observed_at,
                });
                if (!_isBg) {
                  uiModule.showToast('Context compacted — older messages summarized');
                }
              } else if (json.type === 'context_trimmed') {
                if (!_isBg) {
                  const d = json.data || {};
                  const before = Number(d.messages_before || 0);
                  const after = Number(d.messages_after || 0);
                  const detail = before && after && before > after ? ` (${after}/${before} messages sent)` : '';
                  uiModule.showToast(`Context trimmed for this model${detail}`);
                }
              } else if (json.type === 'metrics') {
                metrics = json.data;
                if (!_isBg && holder && metrics) {
                  applyModelMetricsState(metrics, holder, roundHolder, modelName);
                }
                if (_isBg) {
                  var bgM = _backgroundStreams.get(streamSessionId);
                  if (bgM) bgM.metrics = json.data;
                  continue;
                }
                if (metrics) {
                  const metricsTarget = _metricsTargetForTurn();
                  if (metricsTarget) displayMetrics(metricsTarget, metrics);
                  refreshChatContextHeader('metrics');
                }

              } else if (json.type === 'message_saved') {
                // Wire the persisted DB id onto the just-streamed bubble so it
                // can be edited/deleted immediately, without reloading the chat.
                if (_isBg) continue;
                if (holder && json.id) holder.dataset.dbId = json.id;
                if (uiExplanation && holder && json.buddy_help_link) holder._buddyHelpLink = json.buddy_help_link;

              } else if (json.type === 'actor_accounting' || json.type === 'actor_accounting_failure') {
                if (_isBg) continue;
                const actorTarget = _metricsTargetForTurn();
                if (actorTarget) chatRenderer.renderActorAccounting(actorTarget, json.data || {});

              } else if (json.type === 'tool_start') {
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                // Force-close thinking if still open — tools are real content, not thinking
                if (isThinking) {
                  isThinking = false;
                  cancelAnimationFrame(_thinkTimerRAF);
                  var _elapsed2 = thinkingStartTime ? ((Date.now() - thinkingStartTime) / 1000).toFixed(1) : null;
                  if (_liveThinkHeader) _liveThinkHeader.textContent = 'View thinking process';
                  if (_liveThinkTimerEl) _liveThinkTimerEl.textContent = _elapsed2 ? _formatThinkStats(_elapsed2, _liveThinkTokenCount) : '';
                  if (_liveThinkSpinnerSlot) _liveThinkSpinnerSlot.remove();
                  // Assign stable IDs
                  var _thinkId2 = 'think-' + Date.now();
                  var _liveHdr2 = _liveThinkSection && _liveThinkSection.querySelector('.thinking-header');
                  if (_liveHdr2) _liveHdr2.dataset.thinkingId = _thinkId2;
                  if (_liveThinkContent) _liveThinkContent.id = _thinkId2;
                  if (_liveThinkToggle) _liveThinkToggle.id = _thinkId2 + '-toggle';
                }
                _renderStream();
                // --- Finalize current text bubble (only once per round) ---
                if (!roundFinalized) {
                  roundFinalized = true;
                  if (spinner && spinner.element) spinner.destroy();
                  const dt = markdownModule.normalizeThinkingMarkup(_streamDisplayText(roundText));
                  if (dt.trim()) {
                    var _body3 = roundHolder.querySelector('.body');
                    var _contentEl3 = _ensureStreamLayout(_body3);
                    _contentEl3.style.minHeight = '';  // clear streaming inflate
                    _contentEl3.innerHTML = markdownModule.processWithThinking(markdownModule.squashOutsideCode(dt));
                    if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(roundHolder);
                  } else {
                    roundHolder.style.display = 'none';
                  }
                }

                // Track tool name for contextual spinner labels
                _lastToolName = json.tool || '';

                // --- Thread timeline: group tools in a thread container ---
                const cmd = json.command || '';
                const chatBox = document.getElementById('chat-history');
                // Find existing thread to append to — check last few children
                // (agent_step may insert an empty msg-ai between tool rounds)
                let threadWrap = null;
                for (let ci = chatBox.children.length - 1; ci >= Math.max(0, chatBox.children.length - 5); ci--) {
                  const child = chatBox.children[ci];
                  if (child.classList.contains('agent-thread')) {
                    threadWrap = child;
                    break;
                  }
                  // Skip hidden (empty) bubbles and thinking spinners
                  if (child.style.display === 'none' || child.classList.contains('agent-thinking-dots')) continue;
                  // Stop if we hit a visible message bubble (has real content between tools)
                  if (child.classList.contains('msg')) break;
                }
                if (threadWrap) {
                  // Continuing an existing thread — remove has-bottom (agent_step may have set it
                  // expecting text, but we got more tools instead)
                  threadWrap.classList.remove('has-bottom');
                } else {
                  threadWrap = document.createElement('div');
                  threadWrap.className = 'agent-thread';
                  // Extend line up to connect to chat bubble above (if there is one)
                  const _prevSib = chatBox.lastElementChild;
                  const _hasBubbleAbove = _prevSib && (_prevSib.classList.contains('msg') && _prevSib.style.display !== 'none');
                  const _hasThreadAbove = _prevSib && _prevSib.classList.contains('agent-thread');
                  if (_hasBubbleAbove || _hasThreadAbove || (roundText.trim() && roundHolder && roundHolder.style.display !== 'none')) {
                    threadWrap.classList.add('has-top');
                  }
                  chatBox.appendChild(threadWrap);
                }
                threadWrap.classList.add('streaming');
                lastToolThread = threadWrap;
                const toolLabel = _toolLabels[json.tool.toLowerCase()] || json.tool;
                const toolIcon = _toolIcons[json.tool.toLowerCase()] || '\u25B6';
                const node = document.createElement('div')
                node.className = 'agent-thread-node running';
                const cmdHtml = cmd ? `<pre class="agent-thread-cmd">${esc(cmd)}</pre>` : '';
                node.innerHTML = `<div class="agent-thread-dot"></div><div class="agent-thread-header"><span class="agent-thread-icon">${toolIcon}</span><span class="agent-thread-tool">${esc(toolLabel)}</span><span class="agent-thread-wave">▁▂▃</span></div><div class="agent-thread-content">${cmdHtml}</div>`;
                // Expand/collapse via delegated click handler (init at module bottom).
                threadWrap.appendChild(node);
                currentToolBubble = node;
                if (json.id) toolNodesById[json.id] = node;
                // Animate the wave
                const waveEl = node.querySelector('.agent-thread-wave');
                if (waveEl) {
                  const waveFrames = ['▁▂▃', '▂▃▄', '▃▄▅', '▄▅▆', '▅▆▇', '▆▅▄', '▅▄▃', '▄▃▂'];
                  let waveIdx = 0;
                  node._waveInterval = setInterval(() => {
                    waveIdx = (waveIdx + 1) % waveFrames.length;
                    waveEl.textContent = waveFrames[waveIdx];
                  }, 100);
                }
                // Smooth per-second "cooking" timer — ticks every second (not
                // just on the 2s backend heartbeat) so a long-running tool
                // always shows visible motion and never reads as frozen.
                node._startTime = Date.now();
                node._elapsedTicker = setInterval(() => {
                  const hdr2 = node.querySelector('.agent-thread-header');
                  if (!hdr2) return;
                  let el2 = hdr2.querySelector('.agent-thread-elapsed');
                  if (!el2) {
                    el2 = document.createElement('span');
                    el2.className = 'agent-thread-elapsed';
                    // Sits on the LEFT, right after the icon.
                    const icon = hdr2.querySelector('.agent-thread-icon');
                    if (icon && icon.nextSibling) hdr2.insertBefore(el2, icon.nextSibling);
                    else hdr2.appendChild(el2);
                  }
                  const s = (Date.now() - node._startTime) / 1000;
                  // Hundredths so it visibly counts sub-second (1.00, 1.05, …).
                  el2.textContent = s < 60 ? `${s.toFixed(2)}s` : `${Math.floor(s / 60)}m ${(s % 60).toFixed(2).padStart(5, '0')}s`;
                }, 50);
                uiModule.scrollHistory();

              } else if (json.type === 'tool_progress') {
                // Long-running subprocess (bash, python) is still in
                // flight — refresh the running tool card with the
                // elapsed-time + tail of its stdout/stderr so the
                // user doesn't stare at a blind "Running…" spinner.
                if (_isBg) continue;
                const progNode = json.id ? toolNodesById[json.id] : currentToolBubble;
                if (!progNode) continue;
                // The per-second ticker (started in tool_start) owns the
                // elapsed display; here we just surface the live output tail.
                // (homegrown loop sends `tail`, the mimo bridge sends `output`)
                const tailStr = (json.tail || json.output || '').trim();
                if (tailStr) {
                  let tailEl = progNode.querySelector('.agent-thread-tail');
                  if (!tailEl) {
                    tailEl = document.createElement('pre');
                    tailEl.className = 'agent-thread-tail';
                    tailEl.style.cssText = 'margin:4px 0 0;padding:6px 8px;font-size:11px;background:rgba(0,0,0,0.18);border-radius:4px;max-height:140px;overflow:auto;white-space:pre-wrap;opacity:0.85;';
                    const content = progNode.querySelector('.agent-thread-content');
                    if (content) content.appendChild(tailEl);
                  }
                  tailEl.textContent = tailStr;
                  tailEl.scrollTop = tailEl.scrollHeight;
                }
                uiModule.scrollHistory();

              } else if (json.type === 'tool_output') {
                const activity = json.codeActivity;
                if (json.exit_code === 0 && typeof json.id === 'string' && activity?.toolCallID === json.id)
                  observeCodeActivity(activity, {accountID:_sendCapture.owner, chatID:streamSessionId});
                if (_isBg) continue;
                // --- Update THIS call's thread node (by id when present) ---
                const exactToolNode = json.id && toolNodesById[json.id];
                const outNode = json.id ? exactToolNode : currentToolBubble;
                if (json.id) delete toolNodesById[json.id];
                if (outNode) {
                  // Stop wave animation + the per-second cooking ticker
                  if (outNode._waveInterval) {
                    clearInterval(outNode._waveInterval);
                    outNode._waveInterval = null;
                  }
                  if (outNode._elapsedTicker) {
                    clearInterval(outNode._elapsedTicker);
                    outNode._elapsedTicker = null;
                  }
                  const ok = (json.exit_code === 0 || json.exit_code == null);
                  const cmd = json.command || '';
                  let outHtml = '';
                  if (json.output && json.output.trim()) {
                    outHtml = `<details class="agent-tool-output"><summary>Output</summary><pre>${esc(json.output)}</pre></details>`;
                  }
                  // Receipt belongs only to the matching live call, never the
                  // fallback bubble for an unrelated or missing call ID.
                  if (exactToolNode) outHtml += renderSkillUseReceipt(json, esc, {live: true});
                  // File-write diff (write_file): show a before/after unified diff.
                  let diffHtml = '';
                  if (json.diff && json.diff.text) {
                    const d = json.diff;
                    // Collapsed summary: filename + +adds (green) / −dels (red).
                    const stat = [
                      d.new_file ? '<span class="diff-stat-new">new</span>' : '',
                      d.added ? `<span class="diff-stat-add">+${d.added}</span>` : '',
                      d.removed ? `<span class="diff-stat-del">−${d.removed}</span>` : '',
                    ].filter(Boolean).join(' ');
                    const rows = d.text.split('\n').map(line => {
                      let cls = 'diff-ctx', text = line;
                      if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
                      else if (line.startsWith('@@')) cls = 'diff-hunk';
                      // Drop the leading diff marker (+/-/space) — the row colour
                      // already encodes add/del, and keeping it doubles up with
                      // markdown "- " bullets (reads as "+-"/"--").
                      else if (line.startsWith('+')) { cls = 'diff-add'; text = line.slice(1); }
                      else if (line.startsWith('-')) { cls = 'diff-del'; text = line.slice(1); }
                      else if (line.startsWith(' ')) { text = line.slice(1); }
                      return `<span class="${cls}">${esc(text) || '&nbsp;'}</span>`;
                    }).join('');  // spans are display:block — a literal \n here would double-space the diff
                    diffHtml = `<details class="agent-tool-output agent-tool-diff"><summary><span class="diff-file">${esc(d.file || 'diff')}</span> <span class="diff-summary-stats">${stat}</span></summary><pre class="diff-pre">${rows}</pre></details>`;
                  }
                  // For file edits the "command" is the raw JSON args — redundant
                  // next to the diff, so hide it when we have a diff to show.
                  const cmdHtml2 = (cmd && !(json.diff && json.diff.text)) ? `<pre class="agent-thread-cmd">${esc(cmd)}</pre>` : '';
                  // Preserve the user's .open choice across the innerHTML
                  // rewrite \u2014 otherwise expanding a running tool collapses
                  // it as soon as the result lands, forcing the user to
                  // click again. Click handling is delegated (see init at
                  // bottom of file) so no per-node listener needed.
                  const _wasOpen = outNode.classList.contains('open');
                  outNode.className = 'agent-thread-node' + (ok ? '' : ' error') + (_wasOpen ? ' open' : '');
                  outNode.innerHTML = `<div class="agent-thread-dot"></div><div class="agent-thread-header"><span class="agent-thread-icon">${ok ? '\u2713' : '\u2717'}</span><span class="agent-thread-tool">${esc(json.tool)}</span><span class="agent-thread-status">${ok ? 'done' : 'failed'}</span><span class="agent-thread-chevron">\u25B6</span></div><div class="agent-thread-content">${cmdHtml2}${outHtml}${diffHtml}</div>`;
                  const resultButton=mediaResultButton(json.data);
                  if(resultButton)outNode.querySelector('.agent-thread-content')?.appendChild(resultButton);
                  const capturedResource=capturedResourceButton(json.data);
                  if(capturedResource)outNode.querySelector('.agent-thread-content')?.appendChild(capturedResource);
                  const htmlPreview=createCapturedHtmlPreview(json.data);
                  if(htmlPreview)outNode.querySelector('.agent-thread-content')?.appendChild(htmlPreview);
                  const savedFile=resourceResultButton(json.resourceResult);
                  if(savedFile)outNode.querySelector('.agent-thread-content')?.appendChild(savedFile);
                  const capturedButton=capturedChangeButton(json.data?.rawOutput?.metadata?.history?.managed_action_ref);
                  if(capturedButton)outNode.querySelector('.agent-thread-content')?.appendChild(capturedButton);
                  const turnButton=capturedTurnButton(json.data?.rawOutput?.metadata?.history?.managed_action_ref);
                  if(turnButton)outNode.querySelector('.agent-thread-content')?.appendChild(turnButton);
                  const skillSourceButton=exactToolNode ? currentSkillSourceButton(json,{live:true}) : null;
                  if(skillSourceButton)outNode.querySelector('.agent-thread-content')?.appendChild(skillSourceButton);
                  const searchHits = exactToolNode ? searchHitButtons(json, {live:true}) : null;
                  if (searchHits) outNode.querySelector('.agent-thread-content')?.appendChild(searchHits);
                  // Reset so thinking spinner between tools says "Thinking" not the old tool's label
                  _lastToolName = '';
                  uiModule.scrollHistory();
                }
                // --- Render generated images inline ---
                if (json.image_url) {
                  _rememberGeneratedImage(json);
                  _appendGeneratedImageBubble(json);
                }
                // --- Render browser screenshots in tool output ---
                if (json.screenshot && outNode) {
                  const contentEl = outNode.querySelector('.agent-thread-content');
                  if (contentEl) {
                    const screenshotSrc = chatRenderer.safeToolScreenshotSrc(json.screenshot);
                    if (screenshotSrc) {
                      const details = document.createElement('details');
                      details.className = 'agent-tool-output';
                      const summary = document.createElement('summary');
                      summary.textContent = 'Screenshot';
                      const img = document.createElement('img');
                      img.src = screenshotSrc;
                      img.style.cssText = 'max-width:100%;border-radius:6px;margin-top:6px;border:1px solid var(--border)';
                      details.appendChild(summary);
                      details.appendChild(img);
                      contentEl.appendChild(details);
                    }
                  }
                }
                // --- Reload sessions after manage_session tool (delete, rename, etc.) ---
                // Debounce so bulk deletes don't fire loadSessions per call
                if (json.tool === 'manage_session' && sessionModule) {
                  if (window._manageSessionTimer) clearTimeout(window._manageSessionTimer);
                  window._manageSessionTimer = setTimeout(() => sessionModule.loadSessions(), 1000);
                }
                // --- Live-refresh the calendar after manage_calendar (add/edit/delete) ---
                // so a new event shows without the user hard-refreshing. Debounced
                // so a batch of event creates only triggers one refetch.
                if (json.tool === 'manage_calendar') {
                  if (window._manageCalTimer) clearTimeout(window._manageCalTimer);
                  window._manageCalTimer = setTimeout(
                    () => window.dispatchEvent(new CustomEvent('calendar-refresh')), 600);
                }
                // --- Live-refresh Memories after manage_memory changes ---
                if (json.tool === 'manage_memory') {
                  if (window._manageMemoryTimer) clearTimeout(window._manageMemoryTimer);
                  window._manageMemoryTimer = setTimeout(
                    () => window.dispatchEvent(new CustomEvent('memory-refresh')), 600);
                }
                // --- Apply UI control actions embedded in tool_output ---
                if (json.ui_event) {
                  chatStream.handleUIControl(json);
                }
                // Native document tool calls can arrive as a completed
                // tool_output without the text-fence streaming path. Open the
                // document editor from the real doc metadata carried on the
                // tool result so "create a document" never leaves only a chat
                // link behind if the later doc_update event is missed.
                if (
                  documentModule
                  && json.doc_id
                  && ['create_document', 'update_document', 'edit_document'].includes(json.tool)
                ) {
                  documentModule.handleDocUpdate({
                    type: 'doc_update',
                    doc_id: json.doc_id,
                    title: json.document_title || '',
                    language: json.document_language || '',
                    version: json.document_version || 1,
                    content: json.document_content || '',
                  });
                }

                // Schedule a thinking spinner between tool rounds (short delay so
                // agent_step in the same SSE chunk can cancel it before it shows)
                _scheduleThinkingSpinner();
                uiModule.scrollHistory();

              } else if (json.type === 'doc_stream_open') {
                if (_isBg) {
                  // Store for replay when user returns to this session
                  var bgDocOpen = _backgroundStreams.get(streamSessionId);
                  if (bgDocOpen) {
                    bgDocOpen._docTitle = json.title || '';
                    bgDocOpen._docLang = json.language || '';
                    bgDocOpen._docContent = '';
                  }
                  continue;
                }
                if (documentModule) {
                  documentModule.streamDocOpen(json.title || '', json.language || '');
                }

              } else if (json.type === 'doc_stream_delta') {
                if (_isBg) {
                  var bgDocDelta = _backgroundStreams.get(streamSessionId);
                  if (bgDocDelta) bgDocDelta._docContent = json.content || '';
                  continue;
                }
                if (documentModule) {
                  documentModule.streamDocDelta(json.content || '');
                }

              } else if (json.type === 'doc_update') {
                // doc_update means the server already saved the doc to DB.
                if (_isBg) continue;
                if (documentModule) {
                  documentModule.handleDocUpdate(json);
                }

              } else if (json.type === 'doc_suggestions') {
                if (_isBg) continue;
                if (documentModule && documentModule.handleDocSuggestions) {
                  documentModule.handleDocSuggestions(json);
                }

              } else if (json.type === 'ui_control') {
                if (_isBg) continue;
                chatStream.handleUIControl(json.data || {});

              } else if (json.type === 'ask_user') {
                const question = json.data || {};
                if (question.request_id && Array.isArray(question.questions)) {
                  _storeSessionState('question', streamSessionId, question);
                  if (attentionStreamScope && _inputOwner === attentionStreamScope.account && _codeEvidenceAuthEpoch === attentionAuthEpoch) {
                    publishLiveAttention(attentionStreamScope, 'question', streamSessionId, question);
                    chatStream.notifyBackgroundQuestion(attentionStreamScope, streamSessionId, question);
                  }
                }
                if (_isBg) continue;
                // The agent posed a multiple-choice question; the turn has ended.
                // Use the shared history renderer so the live and restored
                // versions have identical behavior.
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                chatRenderer.renderAskUserCard(json.data || {});

              } else if (json.type === 'permission_request') {
                _storeSessionState('permission', streamSessionId, json.data || {});
                document.dispatchEvent(new CustomEvent('odysseus:permission-pending', { detail: { sessionId: streamSessionId, payload: json.data || {} } }));
                if (attentionStreamScope && _inputOwner === attentionStreamScope.account && _codeEvidenceAuthEpoch === attentionAuthEpoch)
                  publishLiveAttention(attentionStreamScope, 'permission', streamSessionId, json.data || {});
                if (_isBg) continue;
                // C1: the agent's turn is blocked server-side until the card
                // answers (no timeout) — keep the stream open and render it.
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                chatRenderer.renderPermissionCard(json.data || {}, streamSessionId);

              } else if (json.type === 'sudo_password_request') {
                // Second prompt of an approved destructive command: the exact
                // command was already cleared; this only collects the password.
                const sudoData = Object.assign({}, json.data || {}, { sudo_password: true });
                _storeSessionState('permission', streamSessionId, sudoData);
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                chatRenderer.renderPermissionCard(sudoData, streamSessionId);

              } else if (json.type === 'plan_update') {
                // Agent wrote back to the plan (ticked a step / revised). Update
                // the stored plan + live-refresh the docked plan window.
                if (json.data) _setStoredPlan(json.data, streamSessionId);
                if (_isBg) continue;

              } else if (json.type === 'protocol_error' || json.type === 'config_error') {
                if (!_isBg) {
                  const detail = json.data || json;
                  uiModule.showError(detail.message || detail.error || 'Open Clank agent protocol error');
                }

              } else if (json.type === 'retry_notice') {
                // Provider hiccup — mimo is waiting out a backoff. Show it
                // instead of a dead spinner.
                if (!_isBg && json.data && json.data.attempt) {
                  const d = json.data;
                  const label = d.maxAttempts
                    ? `Retrying (${d.attempt}/${d.maxAttempts})`
                    : `Retrying (attempt ${d.attempt})`;
                  _replaceThinkingSpinner(label);
                }

              } else if (json.type === 'agent_step') {
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                _renderStream();
                // Mark thread as connected to bubble below
                const _activeThread = document.querySelector('.agent-thread.streaming');
                if (_activeThread) {
                  _activeThread.classList.add('has-bottom');
                }
                // --- New round: create fresh AI bubble with spinner ---
                currentToolBubble = null;
                roundFinalized = false;
                isThinking = false;
                _docFenceOpened = false;
                _docFenceContentStart = -1;
                const box = document.getElementById('chat-history');
                const newWrap = document.createElement('div');
                newWrap.className = 'msg msg-ai msg-continuation streaming';
                // Add model name label
                const newRole = document.createElement('div');
                newRole.className = 'role';
                const metaS = sessionModule.getSessions().find(s => s.id === streamSessionId);
                const _roundRequested = holder?._requestedModel || metaS?.model;
                const _roundActual = holder?._actualModel || _roundRequested;
                newRole.textContent = _modelRouteLabel(_roundRequested, _roundActual) || '';
                _applyModelColor(newRole, _roundActual);
                newWrap.appendChild(newRole);
                const newBody = document.createElement('div');
                newBody.className = 'body';
                newWrap.appendChild(newBody);
                box.appendChild(newWrap);
                roundHolder = newWrap;
                roundText = '';
                // Destroy any previous spinner before creating new one
                if (spinner && spinner.element) spinner.destroy();
                // Show spinner while waiting for text (skip for research — has its own progress)
                if (!_researchingStreamIds.has(streamSessionId)) {
                  spinner = spinnerModule.create('Generating response', 'right', 'wave');
                  newBody.appendChild(spinner.createElement());
                  spinner.start();
                }
                if (streamingTTS) window.aiTTSManager._streamSentencesSent = 0;
                uiModule.scrollHistory();
              } else if (json.type === 'budget_exceeded') {
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                const budgetDiv = document.createElement('div');
                budgetDiv.style.cssText = 'font-size:11px;opacity:0.6;font-style:italic;padding:4px 8px;margin:4px 0;';
                budgetDiv.textContent = `Tool budget reached (${json.used}/${json.limit} calls). Agent stopped.`;
                const chatBox = document.getElementById('chat-history');
                chatBox.appendChild(budgetDiv);

              } else if (json.type === 'loop_breaker_triggered' || json.type === 'intent_nudge_exhausted') {
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                const guardDiv = document.createElement('div');
                guardDiv.className = 'stopped-indicator';
                const guardLabel = document.createElement('span');
                guardLabel.textContent = `[Agent guard: ${json.message || json.reason || 'internal stop'}]`;
                guardDiv.appendChild(guardLabel);
                const targetBody = roundHolder && roundHolder.querySelector('.body');
                if (targetBody) targetBody.appendChild(guardDiv);
                else {
                  const chatBox = document.getElementById('chat-history');
                  if (chatBox) chatBox.appendChild(guardDiv);
                }

              } else if (json.type === 'teacher_takeover') {
                if (_isBg) continue;
                _cancelThinkingTimer();
                _removeThinkingSpinner();
                // Finalize any in-flight bubble so the takeover banner
                // separates student attempt from teacher attempt.
                if (spinner && spinner.element) { try { spinner.destroy(); } catch(_){} spinner = null; }
                const chatBox = document.getElementById('chat-history');
                const banner = document.createElement('div');
                banner.className = 'teacher-takeover-banner';
                banner.style.cssText = 'margin:10px 0;padding:8px 12px;border-left:3px solid #c08a3e;background:rgba(192,138,62,0.08);font-size:12px;color:var(--fg);border-radius:4px;';
                const teacherName = json.teacher_model || 'teacher';
                const why = json.student_failure ? ` &mdash; <span style="opacity:0.7">${esc(json.student_failure)}</span>` : '';
                banner.innerHTML = `<strong>Teacher takeover:</strong> escalating to <code>${esc(teacherName)}</code>${why}`;
                chatBox.appendChild(banner);
                // Reset round bubble state so the teacher's first text starts a new bubble
                roundHolder = null;
                roundText = '';
                roundFinalized = false;
                currentToolBubble = null;
                uiModule.scrollHistory();

              } else if (json.type === 'skill_saved') {
                if (_isBg) continue;
                const chatBox = document.getElementById('chat-history');
                const note = document.createElement('div');
                note.className = 'skill-saved-note';
                note.style.cssText = 'margin:6px 0;padding:6px 10px;border-left:3px solid #4a8a4a;background:rgba(74,138,74,0.07);font-size:12px;color:var(--fg);border-radius:4px;';
                note.innerHTML = `<strong>Skill learned:</strong> <code>${esc(json.name || '')}</code>${json.category ? ` <span style="opacity:0.6">[${esc(json.category)}]</span>` : ''}`;
                chatBox.appendChild(note);
                uiModule.scrollHistory();

              } else if (json.type === 'escalation_failed' || json.type === 'skill_save_failed') {
                if (_isBg) continue;
                const chatBox = document.getElementById('chat-history');
                const note = document.createElement('div');
                note.className = 'escalation-failed-note';
                note.style.cssText = 'margin:6px 0;padding:6px 10px;border-left:3px solid #8a4a4a;background:rgba(138,74,74,0.07);font-size:12px;color:var(--fg);border-radius:4px;';
                const label = json.type === 'escalation_failed' ? 'Teacher could not solve it' : 'Skill not saved';
                note.innerHTML = `<strong>${label}:</strong> <span style="opacity:0.75">${esc(json.reason || '')}</span>`;
                chatBox.appendChild(note);
                uiModule.scrollHistory();

              } else if (json.error) {
                // --- Backend error (timeout, connection issue, etc.) ---
                console.error('Stream error from backend:', json.error);
                if (_isBg) continue;
                if (spinner && spinner.element) spinner.destroy();
                const errDiv = document.createElement('div');
                errDiv.style.cssText = 'color: var(--color-error); font-style: italic; padding: 4px 0;';
                errDiv.textContent = `[Error: ${json.error}]`;
                roundHolder.querySelector('.body').appendChild(errDiv);
                uiModule.scrollHistory();
              }
            } catch (e) {
              console.error('Error parsing SSE data:', e);
            }
          }
        }
      }

      if (_streamTerminalError) {
        throw _streamTerminalError;
      }
      if (!_streamSawDone) {
        throw new Error('Stream closed before completion');
      }

      _renderStream();
      if (spinner && spinner.element) { try { spinner.destroy(); } catch (_) {} spinner = null; }
      _cancelThinkingTimer();
      _removeThinkingSpinner();
      // Stop any thread pulse animations
      document.querySelectorAll('.agent-thread.streaming').forEach(t => t.classList.remove('streaming'));
      // --- Final render (skip if stream was ever backgrounded or currently in background) ---
      // Remove streaming class from all round bubbles
      holder.classList.remove('streaming');
      if (roundHolder && roundHolder !== holder) roundHolder.classList.remove('streaming');

      const _isBgFinal = (sessionModule.getCurrentSessionId() !== streamSessionId) || _backgroundStreams.has(streamSessionId);
      if (!_isBgFinal) {
        finalMeta = sessionModule.getSessions().find(s => s.id === sessionModule.getCurrentSessionId());
        const _finalModelHolder = applyModelMetricsState(metrics, holder, roundHolder, finalMeta?.model) || roundHolder || holder;
        const _finalActualModel = _finalModelHolder?._actualModel || finalMeta?.model;
        const _finalRequestedModel = _finalModelHolder?._requestedModel || finalMeta?.model || _finalActualModel;
        // Prepend character name if set
        var _charNameFinal = presetsModule.getCharacterName ? presetsModule.getCharacterName() : '';
        const roleEl = _finalModelHolder?.querySelector('.role');
        if (roleEl) {
          _setRoleModelLabel(roleEl, _finalRequestedModel, _finalActualModel, {
            suffix: _finalModelHolder._roleSuffix,
            characterName: _charNameFinal || _finalModelHolder._characterName,
          });
        }
        holder.dataset.raw = accumulated;

        // Anti-stall: a turn that ran tools but ended with essentially no
        // final prose usually means the model stopped mid-task (the case
        // where you had to type "did you finish?"). Offer a one-click
        // Continue that resumes exactly where it left off — reuses the same
        // resume mechanism as the user-stop "[Message interrupted]" button.
        try {
          const _usedTools = holder.querySelector('.agent-thread-node');
          const _proseLen = (accumulated || '').replace(/<[^>]*>/g, '').trim().length;
          if (_usedTools && _proseLen < 24 && !holder.querySelector('.agent-continue-btn')) {
            const _stall = document.createElement('div');
            _stall.className = 'stopped-indicator';
            const _lbl = document.createElement('span');
            _lbl.style.cssText = 'font-style:italic;opacity:0.7;';
            _lbl.textContent = 'Paused mid-task';
            _stall.appendChild(_lbl);
            const _cont = document.createElement('button');
            _cont.className = 'continue-btn agent-continue-btn';
            _cont.title = 'Continue — pick up where it left off';
            _cont.textContent = '▸';
            _cont.addEventListener('click', () => {
              _stall.remove();
              const mi = uiModule.el('message');
              if (mi) {
                mi.value = 'Continue — you stopped before finishing. Pick up exactly where you left off and complete the task.';
                const sb = document.querySelector('.send-btn');
                if (sb) sb.click();
              }
            });
            _stall.appendChild(_cont);
            (holder.querySelector('.body') || holder).appendChild(_stall);
          }
        } catch (_) {}

        // Clear streaming minHeight lock
        const _streamContent = roundHolder.querySelector('.stream-content');
        if (_streamContent) _streamContent.style.minHeight = '';
        if (_docFenceOpened) {
          _finishDocumentWritingStatus(roundHolder, true);
          roundHolder.style.display = '';
        }

        // Finalize the last round's bubble — flatten stream-content wrapper for clean DOM
        const finalDisplay = _streamDisplayText(roundText, { final: _docFenceOpened });
        if (finalDisplay.trim()) {
          var _body4 = roundHolder.querySelector('.body');
          // Preserve sources expanded state before final render
          var _wasExpanded = _sourcesExpanded || !!(_body4 && _body4.querySelector('.sources-content.expanded'));

          // If thinking was collapsed in-place during streaming, preserve it
          var _liveReplyEl = _body4 && _body4.querySelector('.live-reply-content');
          var _extracted = _liveReplyEl ? markdownModule.extractThinkingBlocks(finalDisplay) : null;
          var _finalReply = '';
          if (_liveReplyEl) {
            // Try standard extraction first (for native <think> tags)
            if (_extracted?.thinkingBlocks?.length) {
              _finalReply = (_extracted.content || '').trim();
            } else {
              // Non-tag thinking: extract reply from raw text
              // Handle garbled thinking tag: "Thinking: reasoning\n<think>reply"
              const _garbledMatch = finalDisplay.match(/^[\s\S]+?<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>\s*([\s\S]*?)(?:<\/(?:think(?:ing)?|thought)>)?\s*$/i);
              if (_garbledMatch && _garbledMatch[1].trim()) {
                _finalReply = _garbledMatch[1].trim();
              } else {
                // Pure non-tag: find reply boundary by prefix patterns
                const _rs2 = ['Hey', 'Hi ', 'Hi!', 'Hello', 'Sure', 'Yes', 'No ', 'No,', 'Yo', 'OK', 'Here', 'Absolutely', 'Of course', 'Great', 'Alright', 'Thanks', 'Welcome', 'Good ', "I'm happy", "I'd be"];
                const _fr = (finalDisplay || '').trimStart();
                if (markdownModule.startsWithReasoningPrefix(_fr)) {
                  const _fLines = _fr.split('\n');
                  for (let _fi = 1; _fi < _fLines.length; _fi++) {
                    const _fl = _fLines[_fi].trim();
                    if (!_fl) continue;
                    if (_rs2.some(rp => _fl.startsWith(rp))) { _finalReply = _fLines.slice(_fi).join('\n'); break; }
                  }
                  // Within-line check
                  if (!_finalReply) {
                    for (const rp of _rs2) {
                      const rx = new RegExp('[.!?]\\s*(' + rp.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')');
                      const m = rx.exec(_fr);
                      if (m && m.index > 20) { _finalReply = _fr.slice(m.index + 1).trim(); break; }
                    }
                  }
                }
              }
            }
          }
          if (_liveReplyEl && _finalReply) {
            // Render reply into the live-reply container (thinking bar already showing)
            var _replyHtml = markdownModule.mdToHtml(markdownModule.squashOutsideCode(_finalReply));
            _liveReplyEl.innerHTML = _replyHtml;
            _liveReplyEl.classList.remove('live-reply-content');
            if (_sourcesData) {
              var _srcEl = document.createElement('div');
              _srcEl.innerHTML = _buildSourcesBox(_sourcesData, _sourcesType, _wasExpanded);
              _body4.insertBefore(_srcEl.firstChild || _srcEl, _body4.firstChild);
            }
            if (_findingsData) _body4.insertAdjacentHTML('beforeend', chatRenderer.buildFindingsBox(_findingsData));
          } else {
            // Full re-render (reply empty or no live-reply container)
            _body4.innerHTML = (_sourcesData ? _buildSourcesBox(_sourcesData, _sourcesType, _wasExpanded) : '')
              + markdownModule.processWithThinking(markdownModule.squashOutsideCode(finalDisplay))
              + (_findingsData ? chatRenderer.buildFindingsBox(_findingsData) : '');
          }
        } else if (_sourcesHtml) {
          var _body4b = roundHolder.querySelector('.body');
          var _wasExpanded2 = _sourcesExpanded || !!(_body4b && _body4b.querySelector('.sources-content.expanded'));
          _body4b.innerHTML = _sourcesData ? _buildSourcesBox(_sourcesData, _sourcesType, _wasExpanded2) : _sourcesHtml;
        } else if (roundHolder !== holder) {
          // Check if there's thinking content worth showing
          const _thinkingOnly = markdownModule.extractThinkingBlocks(_streamDisplayText(roundText));
          if (_thinkingOnly.thinkingBlocks?.length && !_thinkingOnly.content) {
            // Show thinking in a collapsed section even if no visible reply text
            const _body4c = roundHolder.querySelector('.body');
            if (_body4c) _body4c.innerHTML = markdownModule.processWithThinking(_streamDisplayText(roundText));
          } else {
            roundHolder.style.display = 'none';
            // Thread above expected a bubble below — remove has-bottom since bubble is hidden
            const _lastThread = roundHolder.previousElementSibling;
            if (_lastThread && _lastThread.classList.contains('agent-thread')) {
              _lastThread.classList.remove('has-bottom');
            }
          }
        }


        if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(roundHolder);
        if (markdownModule.renderMermaid) markdownModule.renderMermaid(roundHolder);

        uiModule.scrollHistory();
        // Render RAG sources if present
        if (holder._ragSources && holder._ragSources.length) {
          const details = document.createElement('details');
          details.className = 'rag-sources';
          const summary = document.createElement('summary');
          summary.textContent = `Sources (${holder._ragSources.length} documents)`;
          details.appendChild(summary);
          holder._ragSources.forEach(src => {
            const item = document.createElement('div');
            item.className = 'rag-source-item';
            const _esc = uiModule.esc;
            item.innerHTML = `<strong>${_esc(src.filename)}</strong> <span class="rag-similarity">${(src.similarity * 100).toFixed(1)}%</span><div class="rag-snippet">${_esc(src.snippet)}</div>`;
            details.appendChild(item);
          });
          holder.querySelector('.body').appendChild(details);
        }

        // Hide first bubble if it has no visible text content (e.g. agent went straight to tools)
        if (holder !== roundHolder && holder.style.display !== 'none') {
          const _hBody = holder.querySelector('.body');
          const _hText = _hBody ? _hBody.textContent.trim() : '';
          if (!_hText) holder.style.display = 'none';
        }

        // Attach footer to the last visible bubble (roundHolder for multi-round agent, holder for single)
        const footerTarget = (roundHolder && roundHolder !== holder && roundHolder.style.display !== 'none') ? roundHolder : holder;
        if (holder._buddyHelpLink)
          chatRenderer.addBuddyHandbookLink(footerTarget.querySelector('.body'), holder._buddyHelpLink);
        if (!footerTarget.querySelector('.msg-footer')) {
          footerTarget.appendChild(createMsgFooter(footerTarget));
        }
        if (_generatedImagesForTurn.length && !_isBgFinal) {
          _generatedImagesForTurn.forEach(imgData => _appendGeneratedImageBubble(imgData));
        }
        // Add "View Report" link for completed research
        if (_researchingStreamIds.has(streamSessionId)) {
          _appendViewReportLink(footerTarget, streamSessionId);
        }
	        // Also store raw on the footer target so copy/TTS work. Execute is
	        // attached only when the server has persisted a matching plan state;
	        // streamed text alone is never an approval capability.
	        if (footerTarget !== holder) footerTarget.dataset.raw = accumulated;
	        try {
	          const _endToggles = Storage.loadToggleState();
          if (!uiExplanation && _endToggles.plan_mode && accumulated && streamSessionId) {
            // Draft text is persisted for this session, but remains explicitly
            // unapproved until the user completes the revision/digest CAS.
            fetch(`${API_BASE}/api/session/${encodeURIComponent(streamSessionId)}/plan/draft`, {
              method: 'POST',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ plan: accumulated }),
            }).then(async response => {
              const payload = await response.json().catch(() => ({}));
              if (!response.ok || !payload.plan_state) throw new Error(payload.detail || 'Plan draft sync failed');
              _setStoredPlan(payload.plan_state, streamSessionId);
              if (sessionModule.getCurrentSessionId() === streamSessionId) _attachPlanActions(footerTarget, accumulated);
            }).catch(error => {
              console.warn('Plan draft sync failed', error);
            });
          }
	        } catch (_) {}
	        if (addAITTSButton && accumulated && window.aiTTSManager?._provider !== 'disabled' && window.aiTTSManager?.available) {
	          addAITTSButton(footerTarget, accumulated);
	        }
        // TTS auto-play: streaming mode flushes remaining text, non-streaming enqueues full message
        if (accumulated && window.aiTTSManager && window.aiTTSManager.autoPlay) {
          const ttsBtn = holder.querySelector('.ai-tts-button');
          if (ttsBtn) {
            var ICON_PLAY_TTS = uiIcon("play", 14);
            var ICON_STOP_TTS = uiIcon("stop", 14);
            const resetFn = () => {
              ttsBtn.innerHTML = ICON_PLAY_TTS;
              ttsBtn.classList.remove('playing', 'loading');
              ttsBtn.style.color = '#6b7280';
              ttsBtn.title = 'Read aloud';
            };
            if (streamingTTS) {
              // Flush remaining partial sentence and attach the real button
              window.aiTTSManager.streamingEnd(accumulated);
              window.aiTTSManager.streamingAttachButton(ttsBtn, resetFn);
              // If still playing sentences from the stream, show stop icon
              if (window.aiTTSManager.isPlaying || window.aiTTSManager._processing) {
                ttsBtn.innerHTML = ICON_STOP_TTS;
                ttsBtn.classList.add('playing');
                ttsBtn.style.color = '#ccc';
                ttsBtn.title = 'Stop';
              }
            } else {
              // Non-streaming fallback (autoPlay toggled mid-stream, etc.)
              window.aiTTSManager.enqueue(accumulated, ttsBtn, resetFn);
            }
          }
        }
        if (metrics) {
          displayMetrics(_metricsTargetForTurn() || footerTarget, metrics);
        }
        // Attach variant navigation if this was a regeneration
        _attachVariantNav(footerTarget, replacement?.variants);

        // Merge with previous stopped message if this was a continue
        if (_pendingContinue) {
          const prevEl = _pendingContinue;
          _pendingContinue = null;
          const prevBody = prevEl.querySelector('.body');
          const newBody = footerTarget.querySelector('.body');
          if (prevBody && newBody && prevEl.parentNode) {
            // Merge: combine raw text with *(continued)* marker
            const oldRaw = prevEl.dataset.raw || '';
            const newRaw = footerTarget.dataset.raw || '';
            const mergedRaw = oldRaw + '\n\n*(continued)*\n\n' + newRaw;
            prevEl.dataset.raw = mergedRaw;
            // Re-render merged content
            prevBody.innerHTML = markdownModule.processWithThinking(
              markdownModule.squashOutsideCode(mergedRaw)
            );
            // Remove the new bubble and re-add footer to the merged one
            footerTarget.remove();
            const oldFooter = prevEl.querySelector('.msg-footer');
            if (oldFooter) oldFooter.remove();
            prevEl.appendChild(createMsgFooter(prevEl));
            if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(prevEl);

            // Persist merge to server
            const sid = streamSessionId;
            const mergeNav = sessionModule.getSessionNavigationToken();
            const mergeLeftId = prevEl.dataset.dbId || null;
            const mergeRightId = footerTarget.dataset.dbId || null;
            if (sid) {
              fetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/merge-last-assistant`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ separator: '\n\n*(continued)*\n\n', left_id: mergeLeftId, right_id: mergeRightId })
              }).then(async response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const receipt = await response.json();
                if (!receipt.merged) throw new Error('Merge was not committed');
                if (receipt.message_id && (!Array.isArray(receipt.source_message_ids) || receipt.source_message_ids[0] !== mergeLeftId || receipt.source_message_ids[1] !== mergeRightId)) throw new Error('Merge receipt changed');
                if (sessionModule.getCurrentSessionId() !== sid || sessionModule.getSessionNavigationToken() !== mergeNav || !prevEl.isConnected) return;
                if (receipt.message_id) prevEl.dataset.dbId = receipt.message_id;
              }).catch(error => {
                console.warn('merge-last-assistant failed:', error);
                if (sessionModule.getCurrentSessionId() === sid && sessionModule.getSessionNavigationToken() === mergeNav) {
                  sessionModule.selectSession(sid, { preserveComposer: true });
                }
              });
            }
          }
        }
      } // end if (!_isBgFinal)

    } catch (err) {
      _renderStream();
      // Clean up any active spinner (e.g. "Generating response" during tool calls)
      if (spinner && spinner.element) spinner.destroy();
      _cancelThinkingTimer();
      _removeThinkingSpinner();
      document.querySelectorAll('.agent-thread.streaming').forEach(t => t.classList.remove('streaming'));
      // Check if this stream was running in background
      const _isBgCatch = (sessionModule.getCurrentSessionId() !== streamSessionId) || _backgroundStreams.has(streamSessionId);

      if (_isBgCatch) {
        // Error happened while backgrounded — update map, don't touch DOM
        console.error('Background stream error:', err);
        var bgErr = _backgroundStreams.get(streamSessionId);
        if (bgErr && bgErr.status === 'completed') {
          // [DONE] was already processed — this error is benign (e.g. reader.read() after close)
          // Don't override the completed status; just ensure the completed dot stays
          if (sessionModule && sessionModule.clearStreaming) {
            sessionModule.clearStreaming(streamSessionId);
          }
        } else if (bgErr) {
          bgErr.status = 'error';
          if (sessionModule && sessionModule.clearStreaming) {
            sessionModule.clearStreaming(streamSessionId);
          }
        }
      } else {
        // Stop streaming TTS on any error/abort
        if (streamingTTS && window.aiTTSManager) window.aiTTSManager.stop();

        if (abortCtrl && abortCtrl.signal.aborted) {
          const abortReason = abortCtrl._reason || '';
          if (timedOut || abortReason === 'timeout' || abortReason === 'admission-timeout') {
            const message = abortReason === 'admission-timeout'
              ? 'The Host did not acknowledge startup within 60 seconds. Admission is unknown.'
              : `${streamPhase} reached its wait limit. The connection closed.`;
            if (!reportInterruptedRun(message) && holder) {
              const note = document.createElement('p');
              note.textContent = message;
              holder.querySelector('.body')?.append(note);
            }
            if (currentAbort === abortCtrl) currentAbort = null;
            return;
          }

          if (abortReason === 'offline') {
            const offlineMsg = 'Endpoint offline — switch model or try again.';
            if (holder && !accumulated) {
              holder.querySelector('.body').innerHTML =
                `<div style="color: var(--color-error); font-style: italic; padding: 4px 0;">[${offlineMsg}]</div>`;
            } else if (holder && accumulated) {
              const offlineNote = document.createElement('div');
              offlineNote.className = 'stopped-indicator';
              offlineNote.innerHTML =
                `<span style="color: var(--color-error);">[${offlineMsg}]</span>`;
              holder.querySelector('.body').appendChild(offlineNote);
            }
            if (currentAbort === abortCtrl) currentAbort = null;
            return;
          }

          if (abortReason === 'recovery') {
            const recoveryMsg = 'Streaming was interrupted after the tab went inactive. Partial output was preserved.';
            if (reportInterruptedRun(recoveryMsg)) return;
            if (holder && !accumulated) {
              holder.querySelector('.body').innerHTML =
                `<div style="color: var(--color-error); font-style: italic; padding: 4px 0;">[${recoveryMsg}]</div>`;
            } else if (holder && accumulated) {
              const recoveryNote = document.createElement('div');
              recoveryNote.className = 'stopped-indicator';
              recoveryNote.innerHTML =
                `<span style="color: var(--color-error);">[${recoveryMsg}]</span>`;
              holder.querySelector('.body').appendChild(recoveryNote);
            }
            if (currentAbort === abortCtrl) currentAbort = null;
            return;
          }

          if (abortReason === 'stale-local') {
            const staleMsg = 'The stream connection ended. Original delivery is unverified.';
            if (reportInterruptedRun(staleMsg)) return;
            if (holder && !accumulated) {
              holder.querySelector('.body').innerHTML =
                `<div style="opacity:0.7;font-style:italic;padding:4px 0;">[${staleMsg}]</div>`;
            } else if (holder && accumulated) {
              const staleNote = document.createElement('div');
              staleNote.className = 'stopped-indicator';
              staleNote.innerHTML = `<span style="opacity:0.7;">[${staleMsg}]</span>`;
              holder.querySelector('.body').appendChild(staleNote);
            }
            if (currentAbort === abortCtrl) currentAbort = null;
            return;
          }

          // User-initiated stop (or browser navigation abort).
          // Stopped before any text arrived — keep the bubble as a
          // "Cancelled by user" record (so it survives a refresh).
          if (holder && !accumulated) {
            _renderCancelledBubble(holder);
          }

          // But just in case the stop button didn't render it, render it here
          if (holder && accumulated && !currentHolder && !holder.querySelector('.stopped-indicator')) {
            holder.dataset.raw = accumulated;
            holder.querySelector('.body').innerHTML = markdownModule.processWithThinking(
              markdownModule.squashOutsideCode(accumulated)
            );

            if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(holder);

            const stoppedIndicator = document.createElement('div');
            stoppedIndicator.className = 'stopped-indicator';
            const stoppedLabel = document.createElement('span');
            stoppedLabel.textContent = '[Message interrupted]';
            stoppedIndicator.appendChild(stoppedLabel);
            const continueBtn = document.createElement('button');
            continueBtn.className = 'continue-btn';
            continueBtn.title = 'Continue';
            continueBtn.textContent = '\u25B8';
            continueBtn.addEventListener('click', () => {
              stoppedIndicator.remove();
              _hideUserBubble = true;
              _pendingContinue = holder;
              const cutoff = accumulated;
              const msgInput = uiModule.el('message');
              if (msgInput) {
                msgInput.value = 'Your previous response was interrupted. It ended with:\n\n' + cutoff.slice(-500) + '\n\nDo NOT repeat what you already said. Continue exactly from where you were cut off.';
                const sb = document.querySelector('.send-btn');
                if (sb) sb.click();
              }
            });
            stoppedIndicator.appendChild(continueBtn);
            holder.querySelector('.body').appendChild(stoppedIndicator);

            // Tell server to mark this message as stopped
            const _sid2 = sessionModule.getCurrentSessionId();
            if (_sid2) fetch(`${API_BASE}/api/session/${_sid2}/mark-stopped`, { method: 'POST' }).catch(e => console.warn('mark-stopped failed:', e));

            if (!holder.querySelector('.msg-footer')) {
              holder.appendChild(createMsgFooter(holder));
            }

            uiModule.scrollHistory();
          }

          // Now clear the abort controller
          if (currentAbort === abortCtrl) currentAbort = null;
        } else if (dispatchStarted && (!admissionAccepted || isRecoverableStreamError(err))) {
          const note = admissionAccepted
            ? 'Connection lost while receiving this turn.'
            : 'Connection ended before the Host acknowledged startup. Admission is unknown.';
          if (!reportInterruptedRun(note) && holder) {
            const status = document.createElement('p');
            status.textContent = note;
            holder.querySelector('.body')?.append(status);
          }
        } else if (!admissionAccepted && rejectedDraft) {
          recoverRejectedSend();
          if (sessionModule.getCurrentSessionId() === streamSessionId) {
            uiModule.showError('Unable to send: ' + (err?.message || String(err)));
          }
        } else {
          console.error(err);
          // Early failures (for example a Copal flush error) happen before
          // the response holder exists. Never paint them into an older
          // assistant turn or silently lose them because a global selector
          // found no body; create a response bubble owned by this send.
          let errorHolder = holder?.querySelector('.body') || null;
          if (!errorHolder) {
            const errorBubble = addMessage('assistant', '', finalMeta?.model || '');
            errorHolder = errorBubble?.querySelector('.body') || null;
          }
          if (errorHolder) typewriterInto(errorHolder, `Error: ${err?.message || String(err)}`);
        }
      }
    } finally {
      clearTimeout(admissionTimeout);
      clearResponseTimeout();
      clearProcessingProbe();
      clearFirstTokenWaitTimers();
      _activeStreams.delete(streamSessionId);
      if (_streamSessionId === streamSessionId) _streamSessionId = null;
      _syncForegroundStreamGlobals();
      // Streaming done — let screen readers announce the settled response.
      const _chatLogDone = document.getElementById('chat-history');
      if (_chatLogDone) _chatLogDone.setAttribute('aria-busy', 'false');
      // Any tool card still "running" at stream end never got its
      // tool_output (abort, error, dropped event). Stop its animation
      // timers — leaked intervals from stacked cards freeze the tab —
      // and label it honestly instead of spinning forever.
      document.querySelectorAll('.agent-thread-node.running').forEach((n) => {
        if (n._waveInterval) { clearInterval(n._waveInterval); n._waveInterval = null; }
        if (n._elapsedTicker) { clearInterval(n._elapsedTicker); n._elapsedTicker = null; }
        n.classList.remove('running');
        const wave = n.querySelector('.agent-thread-wave');
        if (wave) wave.remove();
        const hdr = n.querySelector('.agent-thread-header');
        if (hdr && !hdr.querySelector('.agent-thread-status')) {
          const st = document.createElement('span');
          st.className = 'agent-thread-status';
          st.textContent = 'interrupted';
          hdr.appendChild(st);
        }
      });
      // Always clean up research tracking regardless of background state
      _researchingStreamIds.delete(streamSessionId);
      if (_researchingStreamIds.size === 0) {
        var _rToggleCleanup = document.getElementById('research-toggle-btn');
        if (_rToggleCleanup) _rToggleCleanup.classList.remove('research-running');
      }

      // Only reset UI state if still on the stream's session and was never backgrounded
      const _isBgFinally = (sessionModule.getCurrentSessionId() !== streamSessionId) || _backgroundStreams.has(streamSessionId);

      if (!_isBgFinally) {
        // Reset button to idle state
        updateSubmitButton('idle', submitBtn);

        // Re-enable message input; on mobile blur to dismiss keyboard
        if (messageInput) {
          messageInput.disabled = false;
          if (window.innerWidth <= 768) {
            messageInput.blur();
          } else {
            messageInput.focus();
          }
        }

        // Clear tracking variables
        currentAccumulated = '';
        currentHolder = null;
        currentSpinner = null;
        _researchingStreamIds.delete(streamSessionId);
        // Clear research-running highlight if no more active research
        if (_researchingStreamIds.size === 0) {
          var _rToggle2 = document.getElementById('research-toggle-btn');
          if (_rToggle2) _rToggle2.classList.remove('research-running');
        }
        _clearResearchTimer();

        // Re-enable research button and auto-untoggle after use
        // (skip if clarification round — keep toggle on for follow-up)
        const _el = uiModule.el;
        const _researchBtn = _el('research-toggle-btn');
        const _researchToggle = _el('research-toggle');
        if (_researchToggle && _researchToggle.checked) {
          _researchToggle.checked = false;
          Storage.setToggle('research', false);
        }
        if (_researchBtn) {
          _researchBtn.disabled = false;
          _researchBtn.classList.remove('active');
          _researchBtn.style.display = 'none';
        }
        // Also sync overflow and tool sidebar buttons
        const _overflowRes = _el('overflow-research-btn');
        if (_overflowRes) _overflowRes.classList.remove('active');
        const _toolRes = _el('tool-research-btn');
        if (_toolRes) _toolRes.classList.remove('active');

      }

      // Research clarification timeout — if user doesn't reply within 5 min, show timeout
      if (holder && holder._roleSuffix === 'Research' && !_researchingStreamIds.has(streamSessionId)) {
        var _timeoutSessionId = streamSessionId;
        var _timeoutTimer = setTimeout(async function() {
          // Check if research_pending is still active (user hasn't replied)
          try {
            var _box = document.getElementById('chat-history');
            if (_box && sessionModule.getCurrentSessionId() === _timeoutSessionId) {
              var _timeoutMsg = document.createElement('div');
              _timeoutMsg.className = 'msg msg-ai';
              _timeoutMsg.innerHTML = '<div class="role">Open Clank</div><div class="body" style="opacity:0.6;font-style:italic;">Research clarification timed out. Toggle research again to start over.</div>';
              _box.appendChild(_timeoutMsg);
              uiModule.scrollHistory();
            }
          } catch(_te) {}
        }, 5 * 60 * 1000);
        // Cancel timeout if user sends a message
        var _origSubmit = window._researchTimeoutTimer;
        if (_origSubmit) clearTimeout(_origSubmit);
        window._researchTimeoutTimer = _timeoutTimer;
      }

      // Release Web Lock
      if (_webLockRelease) {
        _webLockRelease();
        _webLockRelease = null;
      }

      // Refresh session list after a delay (picks up auto-generated names)
      setTimeout(() => {
        if (sessionModule && sessionModule.loadSessions) {
          sessionModule.loadSessions();
        }
      }, 3000);
      _refreshComposerQueue();
    }
  }

  /**
   * Abort current chat request
   */
  // stopServer=true ONLY for an explicit user Stop. The run is now DETACHED
  // (survives tab close / navigation), so the generic abort used by cleanup
  // paths (session switch, delete, reader teardown on tab close) must NOT stop
  // the server run — otherwise closing the tab would kill the background task,
  // defeating the whole point. Only the Stop button cancels the server run.
  export function abortCurrentRequest(stopServer = false) {
    const active = _getForegroundStreamState();
    const abortCtrl = active ? active.abortCtrl : currentAbort;
    if (abortCtrl) {
      abortCtrl.abort();
      // Don't set to null here - let catch block handle it
    }
    if (stopServer) {
      try {
        const _sid = (sessionModule && sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId())
          || _streamSessionId
          || (window.sessionModule && window.sessionModule.getCurrentSessionId && window.sessionModule.getCurrentSessionId());
        if (_sid) {
          _stopDetachedRun(_sid);
        }
      } catch (_) {}
    }
  }

  function _showOriginalStreamOutcome(holder, sessionId, runId, message) {
    const scope = _durableInputScope();
    if (!scope || scope.chat !== sessionId || !holder?.isConnected) return false;
    const current = () => holder.isConnected && _durableInputScopeCurrent(scope);
    const scopeEpoch = _codeEvidenceAuthEpoch;
    const panel = document.createElement('div');
    panel.className = 'stopped-indicator';
    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    status.textContent = message;
    panel.append(status);
    if (runId) {
      const check = document.createElement('button');
      check.type = 'button';
      check.textContent = 'Reconnect to original';
      const stop = document.createElement('button');
      stop.type = 'button';
      stop.textContent = 'Stop original';
      const busy = value => { check.disabled = value; stop.disabled = value; };
      check.addEventListener('click', async () => {
        if (!current()) return;
        busy(true);
        const attached = await resumeStream(sessionId, {expectedRunId:runId, replaceHolder:holder});
        if (current() && !attached) {
          status.textContent = 'The original run is unavailable. Its last outcome could not be verified.';
          busy(false);
        }
      });
      stop.addEventListener('click', async () => {
        if (!current()) return;
        busy(true);
        status.textContent = 'Waiting for the Host to acknowledge Stop…';
        try {
          const result = await fetch(`${API_BASE}/api/chat/stop/${encodeURIComponent(sessionId)}`, {
            method:'POST', credentials:'same-origin', headers:{'X-Agent-Run-ID':runId}, signal:AbortSignal.timeout(10_000),
          });
          const receipt = await result.json();
          if (!current()) return;
          status.textContent = result.ok && receipt.run_id === runId && receipt.stopped === true
            ? 'The Host acknowledged Stop. Reconnect to check the original outcome.'
            : 'The Host could not confirm Stop for the original run.';
        } catch {
          if (current()) status.textContent = 'Stop acknowledgement is unavailable. Check the original run.';
        } finally {
          if (current()) busy(false);
        }
      });
      panel.append(check, stop);
    } else {
      const check = document.createElement('button');
      check.type = 'button';
      check.textContent = 'Check saved chat';
      check.addEventListener('click', async () => {
        if (!current()) return;
        check.disabled = true;
        status.textContent = 'Opening the saved transcript for this chat…';
        try {
          await sessionModule.selectSession(sessionId, {preserveComposer:true});
          if (_inputOwner === scope.account && _codeEvidenceAuthEpoch === scopeEpoch
              && sessionModule.getCurrentSessionId() === sessionId) {
            uiModule.showToast?.('Check the saved transcript for the interrupted turn. Its operation ID was unavailable.');
          }
        } catch {
          if (current()) uiModule.showError?.('The saved transcript is unavailable.');
        } finally {
          if (current()) { check.disabled = false; status.textContent = message; }
        }
      });
      panel.append(check);
    }
    (holder.querySelector('.body') || holder).append(panel);
    return true;
  }

  function _removeStallBanner() {
    const b = document.getElementById('stall-banner');
    if (b) b.remove();
    _stallBannerShown = false;
  }
  function _showStallBanner(secs) {
    if (document.getElementById('stall-banner')) return;
    _stallBannerShown = true;
    const box = document.getElementById('chat-history');
    if (!box) return;
    const bar = document.createElement('div');
    bar.id = 'stall-banner';
    bar.className = 'stall-banner';
    const mins = Math.floor(secs / 60);
    const label = mins >= 1 ? `${mins}m` : `${secs}s`;
    bar.innerHTML = `<span class="stall-banner-txt">Quiet for ${label} — still working?</span>`;
    const cont = document.createElement('button');
    cont.className = 'stall-banner-btn';
    cont.textContent = 'Nudge it';
    cont.title = 'Stop the stalled stream and ask it to continue';
    cont.addEventListener('click', () => {
      _removeStallBanner();
      const mi = uiModule.el('message');
      if (mi) {
        mi.value = 'Are you still working? If you stopped, continue exactly where you left off and finish the task.';
        const sb = document.querySelector('.send-btn');
        if (sb) sb.click();
      }
    });
    const stop = document.createElement('button');
    stop.className = 'stall-banner-btn stall-banner-stop';
    stop.textContent = 'Stop';
    stop.addEventListener('click', () => { _removeStallBanner(); abortCurrentRequest(true); });
    bar.appendChild(cont);
    bar.appendChild(stop);
    box.appendChild(bar);
    if (uiModule.scrollHistory) uiModule.scrollHistory();
  }
  async function _probeStaleLocalStream() {
    const active = _getForegroundStreamState();
    if (!active || _staleStreamProbeInFlight) return;
    if (Date.now() - (active.lastActivity || _lastReaderActivity) < STALE_LOCAL_STREAM_MS) return;
    const sid = sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId();
    if (!sid) return;
    if (_backgroundStreams.has(sid) || (sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId() !== sid)) return;
    _staleStreamProbeInFlight = true;
    try {
      const res = await fetch(`${API_BASE}/api/chat/stream_status/${encodeURIComponent(sid)}`, {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!_getForegroundStreamState() || _backgroundStreams.has(sid)) return;
      if (res.status !== 404) return;

      console.warn('[stream-watchdog] Local stream was stale and server has no active stream. Unlocking composer.');
      if (active.abortCtrl && !active.abortCtrl.signal.aborted) {
        active.abortCtrl._reason = 'stale-local';
        active.abortCtrl.abort();
      }
      _activeStreams.delete(sid);
      _syncForegroundStreamGlobals();
      _sendInFlight = false;
      if (_webLockRelease) {
        _webLockRelease();
        _webLockRelease = null;
      }
      const submitBtn = document.querySelector('.send-btn');
      if (submitBtn) updateSubmitButton('idle', submitBtn);
      const messageInput = uiModule.el('message');
      if (messageInput) messageInput.disabled = false;
      _refreshComposerQueue();
    } catch (err) {
      console.warn('[stream-watchdog] Stream status probe failed:', err);
    } finally {
      _staleStreamProbeInFlight = false;
    }
  }

  function _startStallWatchdog() {
    // Keep the old noisy stall banner disabled. This watchdog only unlocks
    // a dead local stream after the backend confirms no active stream exists.
    if (_stallWatchdog) { clearInterval(_stallWatchdog); _stallWatchdog = null; }
    _removeStallBanner();
    _stallWatchdog = setInterval(_probeStaleLocalStream, 5000);
  }
  function _stopStallWatchdog() {
    if (_stallWatchdog) { clearInterval(_stallWatchdog); _stallWatchdog = null; }
    _removeStallBanner();
  }

  /** Show a "Cancelled by user" record in `holder` and persist an empty
   *  assistant placeholder server-side so the turn survives a refresh.
   *  Called from both abort paths when no tokens had streamed yet. */
  function _renderCancelledBubble(holder) {
    if (!holder) return;
    // The Stop click path and the aborted fetch catch can both reach this
    // helper. Make the DOM marker and persistence write idempotent so one
    // user action cannot create duplicate cancelled assistant rows.
    if (holder.dataset.cancelledByUser === 'true') return;
    holder.dataset.cancelledByUser = 'true';
    holder.dataset.raw = '';
    const body = holder.querySelector('.body');
    if (body) {
      body.innerHTML = '';
      const indicator = document.createElement('div');
      indicator.className = 'stopped-indicator';
      const label = document.createElement('span');
      label.style.fontStyle = 'italic';
      label.style.opacity = '0.7';
      label.textContent = '[Cancelled by user]';
      indicator.appendChild(label);
      body.appendChild(indicator);
    }
    if (typeof createMsgFooter === 'function' && !holder.querySelector('.msg-footer')) {
      holder.appendChild(createMsgFooter(holder));
    }
    // Persist as an assistant message with stopped+cancelled metadata so the
    // chat-history loader renders the same indicator after a refresh.
    // Include the model name so the bubble header still shows which model
    // was running when the user hit Stop.
    const sid = sessionModule.getCurrentSessionId();
    if (sid) {
      let modelName = '';
      try { modelName = sessionModule.getCurrentModel?.() || ''; } catch {}
      // Fallback: pull from the holder's existing meta (the streaming
      // placeholder usually has the model set in the header already).
      if (!modelName) {
        modelName = holder.dataset.model
          || holder.querySelector('.msg-header .msg-model')?.textContent
          || '';
      }
      fetch(`${API_BASE}/api/session/${sid}/inject_messages`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [{
            role: 'assistant',
            content: '',
            metadata: { stopped: true, cancelled: true, model: modelName },
          }],
        }),
      }).catch(() => {});
    }
  }

  /**
   * Detach current stream to run in background instead of aborting.
   * Called when user switches sessions mid-stream.
   */
  export function detachCurrentStream(sessionId) {
    const active = sessionId ? _activeStreams.get(sessionId) : _getForegroundStreamState();
    if (!active || !active.abortCtrl) {
      // Not streaming — fall through to abort
      abortCurrentRequest();
      return;
    }
    // Store background stream state
    _backgroundStreams.set(sessionId, {
      status: 'running',
      accumulated: currentAccumulated,
      sourcesHtml: '',
      findingsData: null,
      abortCtrl: active.abortCtrl,
      query: active.query || (active.holder ? (active.holder._researchQuery || '') : ''),
      metrics: null,
      runId: active.runId || '',
    });
    // Mark session with pulsing dot in sidebar
    if (sessionModule && sessionModule.markStreaming) {
      sessionModule.markStreaming(sessionId);
    }
    // Clear local state WITHOUT aborting the fetch
    if (currentAbort === active.abortCtrl) currentAbort = null;
    if (currentHolder === active.holder) currentHolder = null;
    if (_streamSessionId === sessionId) _streamSessionId = null;
    currentAccumulated = '';
    _syncForegroundStreamGlobals();
    // Reset submit button so the new chat is ready to send
    const submitBtn = document.querySelector('.send-btn');
    if (submitBtn) updateSubmitButton('idle', submitBtn);
  }

  // _notifyStreamComplete and _insertStreamDoneToast now in chatStream.js
  var _notifyStreamComplete = chatStream.notifyStreamComplete;
  var _insertStreamDoneToast = chatStream.insertStreamDoneToast;

  /**
   * Live-resume a chat run still streaming detached on the server (#2539).
   *
   * On session re-entry, GET /api/chat/resume/{id} replays the run's buffer then
   * streams live; reply tokens render as they arrive. On completion a plain text
   * reply is finalized in place (canonical bubble via chatRenderer.addMessage, no
   * reload); a "rich" reply (tool calls, sources, doc streaming, multi-round) is
   * reloaded from the DB so its full render stays faithful. Returns true if it
   * attached, false to let the caller fall back to spinner+poll.
   */
  export async function resumeStream(sessionId, {expectedRunId = '', replaceHolder = null} = {}) {
    const attentionScope = captureContinuityScope(), attentionAuthEpoch = _codeEvidenceAuthEpoch;
    if (!sessionId) return false;
    if (hasActiveStream(sessionId)) return false;

    // Claim the session before the first await.  Refresh/session-switch hooks
    // can call resumeStream concurrently; checking then fetching before taking
    // this lock lets both callers attach and render the same detached run.
    _resumingStreams.set(sessionId, '');

    const navigation = sessionModule.getSessionNavigationToken();
    const originalScope = expectedRunId ? _durableInputScope() : null;
    const stillSelected = () => sessionModule.getCurrentSessionId() === sessionId
      && sessionModule.getSessionNavigationToken() === navigation && _codeEvidenceAuthEpoch === attentionAuthEpoch
      && (!expectedRunId || _durableInputScopeCurrent(originalScope));
    const reconnectController = new AbortController();
    const reconnectTimeout = setTimeout(() => reconnectController.abort(), 10_000);
    let res;
    try {
      if (!stillSelected()) { _resumingStreams.delete(sessionId); return false; }
      res = await fetch(`${API_BASE}/api/chat/resume/${encodeURIComponent(sessionId)}`, {
        credentials:'same-origin', signal:reconnectController.signal,
        ...(expectedRunId ? {headers:{'X-Agent-Run-ID':expectedRunId}} : {}),
      });
    } catch {
      _resumingStreams.delete(sessionId);
      return false;
    } finally { clearTimeout(reconnectTimeout); }
    const resumedId = res.headers.get('X-Agent-Run-ID') || '';
    if (!res.ok || !res.body || !stillSelected() || (expectedRunId && resumedId !== expectedRunId)) {
      _resumingStreams.delete(sessionId);
      try { await res.body?.cancel(); } catch (_) {}
      return false;
    }
    _resumingStreams.set(sessionId, resumedId);

    const box = document.getElementById('chat-history');
    if (!box) {
      _resumingStreams.delete(sessionId);
      try { await res.body.cancel(); } catch (_) {}
      return false;
    }

    // A dedicated set (not _backgroundStreams) so checkBackgroundStream
    // doesn't mistake this for a same-tab POST stream and spawn its own
    // spinner+poll on re-entry.

    const holder = document.createElement('div');
    holder.className = 'msg msg-ai';
    const meta = sessionModule.getSessions().find(s => s.id === sessionId);
    const roleLabel = _shortModel(meta && meta.model);
    const roleTs = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    holder.innerHTML = '<div class="role">' + uiModule.esc(roleLabel) +
      ' <span class="role-timestamp">' + roleTs + '</span></div>' +
      '<div class="body"><div class="stream-content"></div></div>';
    _applyModelColor(holder.querySelector('.role'), meta && meta.model);
    const contentDiv = holder.querySelector('.stream-content');
    if (replaceHolder?.parentElement === box) replaceHolder.replaceWith(holder);
    else box.appendChild(holder);

    const spinner = spinnerModule.create('Generating response...', 'right');
    holder.querySelector('.body').appendChild(spinner.createElement());
    spinner.start();
    uiModule.scrollHistory();

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let roundText = '';
    let docFenceOpened = false;
    let gotDelta = false;
    let leftSession = false;
    let metricsData = null;
    // "Rich" responses (tool calls, sources, doc streaming, multi-round) need the
    // full canonical render, which is rebuilt from the saved DB record on reload.
    // Plain text replies can be finalized in place without a reload.
    let rich = false;

    const cleanup = () => {
      try { spinner.destroy(); } catch (_) {}
      _resumingStreams.delete(sessionId);
    };

    const renderDelta = () => {
      const dt = markdownModule.normalizeThinkingMarkup(_streamDisplayText(roundText, { final: docFenceOpened }));
      if (docFenceOpened && !dt.trim()) {
        _showDocumentWritingStatus(contentDiv);
      } else {
        contentDiv.innerHTML = markdownModule.mdToHtml(markdownModule.squashOutsideCode(dt));
      }
      uiModule.scrollHistory();
    };

    try {
      readLoop:
      while (true) {
        // User left this session: stop rendering, the run continues server-side.
        if (sessionModule.getCurrentSessionId &&
            sessionModule.getCurrentSessionId() !== sessionId) {
          leftSession = true;
          try { await reader.cancel(); } catch (_) {}
          break;
        }
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        for (const part of parts) {
          const line = part.split('\n').find(l => l.startsWith('data: '));
          if (!line) continue;
          const payload = line.slice(6);
          if (payload === '[DONE]') {
            try { await reader.cancel(); } catch (_) {}
            break readLoop;
          }
          let json;
          try { json = JSON.parse(payload); } catch (_) { continue; }
          if (json.type==='compacted' && resumedId) {
            reportCodeContextBoundary(chatCodeBoundaryScope(sessionId,attentionScope.account),{
              operation:'compact',eventID:'auto:'+resumedId,observedAt:json.observed_at,
            });
          } else if (json.type==='tool_output' && json.exit_code===0 && typeof json.id==='string' && json.codeActivity?.toolCallID===json.id) {
            observeCodeActivity(json.codeActivity,{accountID:attentionScope.account,chatID:sessionId});
          }
          if (json.delta) {
            roundText += json.delta;
            if (!docFenceOpened && (roundText.includes('```create_document\n') || roundText.includes('```document\n') || roundText.includes('```documen\n'))) {
              docFenceOpened = true;
              rich = true;
            }
            if (!gotDelta) { gotDelta = true; try { spinner.destroy(); } catch (_) {} }
            renderDelta();
          } else if (json.type === 'doc_stream_open') {
            rich = true;
            if (documentModule) documentModule.streamDocOpen(json.title || '', json.lang || '');
          } else if (json.type === 'doc_stream_delta') {
            rich = true;
            if (documentModule) documentModule.streamDocDelta(json.content || json.delta || '');
          } else if (json.type === 'metrics') {
            metricsData = json.data || metricsData;
          } else if (json.type === 'actor_accounting' || json.type === 'actor_accounting_failure') {
            chatRenderer.renderActorAccounting(holder, json.data || {});
          } else if (json.type === 'ask_user') {
            const question = json.data || {};
            if (question.request_id && Array.isArray(question.questions)) {
              _storeSessionState('question', sessionId, question);
              if (_inputOwner === attentionScope.account && sessionId === attentionScope.chat && _codeEvidenceAuthEpoch === attentionAuthEpoch) {
                publishLiveAttention(attentionScope, 'question', sessionId, question);
                chatStream.notifyBackgroundQuestion(attentionScope, sessionId, question);
              }
            }
            try { spinner.destroy(); } catch (_) {}
            chatRenderer.renderAskUserCard(json.data || {});
          } else if (json.type === 'permission_request') {
            _storeSessionState('permission', sessionId, json.data || {});
            document.dispatchEvent(new CustomEvent('odysseus:permission-pending', { detail: { sessionId, payload: json.data || {} } }));
            if (_inputOwner === attentionScope.account && sessionId === attentionScope.chat && _codeEvidenceAuthEpoch === attentionAuthEpoch)
              publishLiveAttention(attentionScope, 'permission', sessionId, json.data || {});
            try { spinner.destroy(); } catch (_) {}
            chatRenderer.renderPermissionCard(json.data || {}, sessionId);
          } else if (json.type === 'sudo_password_request') {
            const sudoData = Object.assign({}, json.data || {}, { sudo_password: true });
            _storeSessionState('permission', sessionId, sudoData);
            try { spinner.destroy(); } catch (_) {}
            chatRenderer.renderPermissionCard(sudoData, sessionId);
          } else if (json.type === 'tool_start' || json.type === 'tool_output' ||
                     json.type === 'tool_progress') {
            rich = true;
            // A tool-heavy agent run emits no text for minutes; without this
            // the re-attached view is a blind spinner that reads as a dead
            // turn. Show a lightweight activity feed (id-paired, no timers) —
            // the full tool cards come from the DB reload when the run ends.
            try { spinner.destroy(); } catch (_) {}
            let feed = holder.querySelector('.resume-tool-feed');
            if (!feed) {
              feed = document.createElement('div');
              feed.className = 'resume-tool-feed';
              feed.style.cssText = 'font-size:11px;opacity:0.6;font-family:monospace;padding:2px 0;';
              holder.querySelector('.body').appendChild(feed);
            }
            if (json.type === 'tool_start') {
              const row = document.createElement('div');
              row.dataset.toolId = json.id || '';
              row.textContent = '▸ ' + (json.tool || 'tool');
              feed.appendChild(row);
            } else if (json.type === 'tool_output') {
              const row = json.id ? feed.querySelector(`[data-tool-id="${CSS.escape(json.id)}"]`) : feed.lastElementChild;
              const ok = (json.exit_code === 0 || json.exit_code == null);
              if (row) row.textContent = (ok ? '✓ ' : '✗ ') + (json.tool || 'tool');
            }
            uiModule.scrollHistory();
          } else if (json.type === 'agent_step' || (json.type === 'message_saved' && json.buddy_help_link) ||
                     json.type === 'web_sources' || json.type === 'rag_sources' ||
                     json.type === 'research_progress' || json.type === 'research_sources' ||
                     json.type === 'research_findings' || json.type === 'research_done') {
            rich = true;
          }
        }
      }
    } catch (e) {
      // Network drop or parse failure: fall through to the reload below.
    }

    cleanup();
    if (docFenceOpened) _finishDocumentWritingStatus(holder, true);
    if (leftSession) { if (holder.parentNode) holder.remove(); return true; }

    const onThisSession = sessionModule.getCurrentSessionId &&
                          sessionModule.getCurrentSessionId() === sessionId;

    // Plain text reply: finalize in place. Replace the live bubble with a
    // canonical single message (markdown + footer actions + metrics) using the
    // same renderer history does. No history refetch, no end-of-stream flicker.
    if (onThisSession && !rich && roundText.trim()) {
      if (holder.parentNode) holder.remove();
      const model = meta && meta.model;
      const meta_ = metricsData ? Object.assign({ model }, metricsData) : { model };
      chatRenderer.addMessage('assistant', roundText, model, meta_);
      uiModule.scrollHistory();
      return true;
    }

    // Rich response (tools, sources, docs, multi-round) or user moved on:
    // reload from the DB for the full canonical render.
    if (holder._docWritingThread && holder._docWritingThread.parentNode) holder._docWritingThread.remove();
    if (holder.parentNode) holder.remove();
    if (onThisSession) sessionModule.selectSession(sessionId);
    else sessionModule.loadSessions();
    return true;
  }

  /**
   * Check for background streams when switching to a session.
   * Called after history loads on session switch.
   */
  export function checkBackgroundStream(sessionId) {
    if (!sessionId || !_backgroundStreams.has(sessionId)) return;
    var entry = _backgroundStreams.get(sessionId);

    if (entry.status === 'completed') {
      // Response is already saved to DB and will appear in history — just clean up
      _backgroundStreams.delete(sessionId);
      return;
    }

    if (entry.status === 'error') {
      _backgroundStreams.delete(sessionId);
      var box = document.getElementById('chat-history');
      if (box) {
        var errHolder = document.createElement('div');
        errHolder.className = 'msg msg-ai';
        errHolder.innerHTML = '<div class="body"><i style="color: var(--color-error);">[Background stream encountered an error]</i></div>';
        box.appendChild(errHolder);
      }
      return;
    }

    if (entry.status === 'running') {
      // Stream is still active — show a clean spinner, poll until done,
      // then reload history to show the final saved response.
      var box = document.getElementById('chat-history');
      if (!box) return;

      // Replay any doc content that was streamed in the background
      if (entry._docTitle != null && documentModule) {
        documentModule.streamDocOpen(entry._docTitle, entry._docLang || '');
        if (entry._docContent) {
          documentModule.streamDocDelta(entry._docContent);
        }
      }

      var holder = document.createElement('div');
      holder.className = 'msg msg-ai';
      var meta = sessionModule.getSessions().find(function(s) { return s.id === sessionId; });
      var roleLabel = _shortModel(meta && meta.model);
      var roleTs = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
      holder.innerHTML = '<div class="role">' + uiModule.esc(roleLabel) + ' <span class="role-timestamp">' + roleTs + '</span></div><div class="body"></div>';
      _applyModelColor(holder.querySelector('.role'), meta && meta.model);

      var bodyDiv = holder.querySelector('.body');
      var spinner = spinnerModule.create('Response streaming in background', 'right');
      bodyDiv.appendChild(spinner.createElement());
      spinner.start();

      box.appendChild(holder);
      uiModule.scrollHistory();

      // Poll map until stream finishes, then reload history
      var pollId = setInterval(function() {
        if (sessionModule.getCurrentSessionId() !== sessionId) {
          clearInterval(pollId);
          spinner.destroy();
          if (holder.parentNode) holder.remove();
          return;
        }
        // Update doc content while polling
        var curPoll = _backgroundStreams.get(sessionId);
        if (curPoll && curPoll._docContent && documentModule) {
          documentModule.streamDocDelta(curPoll._docContent);
        }
        if (!curPoll || curPoll.status !== 'running') {
          clearInterval(pollId);
          spinner.destroy();
          if (holder.parentNode) holder.remove(); // Remove entire holder, not just spinner
          _backgroundStreams.delete(sessionId);
          // Reload session to show the completed response — but only if the user
          // is still on it; don't yank them back from a new chat they opened.
          if (sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId() === sessionId) {
            sessionModule.selectSession(sessionId);
          } else {
            sessionModule.loadSessions();
          }
        }
      }, 500);
    }
  }

  // Tag short single-line code blocks with .pre-compact so the CSS can
  // render the Run/Edit/Copy buttons as a slim row that doesn't make a
  // 1-line bash block taller than its own contents.
  function _markCompactPre(pre) {
    const code = pre.querySelector('code');
    if (!code) return;
    const txt = code.textContent || '';
    // Count visible lines — ignore trailing newline (common with fenced
    // blocks) and treat any empty extra line as not a real second line.
    const lines = txt.replace(/\n+$/, '').split('\n');
    const compact = lines.length <= 1 && txt.length < 200;
    pre.classList.toggle('pre-compact', compact);
  }
  function _scanCompactPres(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('pre').forEach(_markCompactPre);
  }
  // Global observer so any <pre> added anywhere in the app (chat stream,
  // chat re-renders, document library chat previews, slash commands,
  // research previews, etc.) gets tagged without each call site needing
  // to remember.
  (function _initCompactPreObserver() {
    if (window._cmpPreObserverWired) return;
    window._cmpPreObserverWired = true;
    _scanCompactPres(document.body);
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.tagName === 'PRE') _markCompactPre(n);
          if (n.querySelectorAll) _scanCompactPres(n);
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  })();

  /**
   * Initialize event listeners
   */
  export function initListeners() {
    // Global event delegation for copy-code buttons
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.copy-code');
      if (!btn) return;
      e.stopPropagation();
      const code = btn.getAttribute('data-code');
      if (code && uiModule) {
        uiModule.copyToClipboard(code);
        // Visual feedback: swap the icon to a checkmark (regular size)
        // and add .copied which the CSS uses to flash green + pulse.
        // For slim/.pre-compact buttons the label text comes from a
        // CSS ::before — swap it via data-state so we don't break the
        // text-button layout.
        const origHTML = btn.innerHTML;
        const isCompact = !!btn.closest('pre.pre-compact');
        if (!isCompact) {
          btn.innerHTML = uiIcon("check", 14, {"role":"success"});
        }
        btn.classList.add('copied');
        btn.dataset.state = 'copied';
        setTimeout(() => {
          if (!isCompact) btn.innerHTML = origHTML;
          btn.classList.remove('copied');
          delete btn.dataset.state;
        }, 1500);
      }
    });

    // Run code button delegation
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.run-code');
      if (!btn) return;
      e.stopPropagation();
      if (codeRunnerModule) codeRunnerModule.run(btn);
    });

    // Edit code button delegation — toggle contentEditable on the code element
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.edit-code');
      if (!btn) return;
      e.stopPropagation();
      const pre = btn.closest('pre');
      if (!pre) return;
      const codeEl = pre.querySelector('code');
      if (!codeEl) return;
      const isEditing = codeEl.contentEditable !== 'false' && codeEl.contentEditable !== 'inherit';
      if (isEditing) {
        // Save: exit edit mode, update data-code on copy/run buttons
        codeEl.contentEditable = 'false';
        codeEl.classList.remove('editing');
        pre.classList.remove('editing');
        const newCode = codeEl.textContent;
        const copyBtn = pre.querySelector('.copy-code');
        if (copyBtn) copyBtn.setAttribute('data-code', newCode);
        const runBtn = pre.querySelector('.run-code');
        if (runBtn) runBtn.setAttribute('data-code', newCode);
        // Swap icon back to pencil
        btn.innerHTML = uiIcon("edit", 14);
        btn.title = 'Edit';
        btn.classList.remove('active');
      } else {
        // Enter edit mode. Firefox (especially on mobile) historically lacks
        // contentEditable="plaintext-only" — setting it there leaves the block
        // non-editable, so the tap "just gets a checkmark" with no way to type.
        // Fall back to "true" when plaintext-only didn't take.
        try { codeEl.contentEditable = 'plaintext-only'; } catch (_) { /* unsupported value */ }
        if (codeEl.contentEditable !== 'plaintext-only') codeEl.contentEditable = 'true';
        codeEl.classList.add('editing');
        pre.classList.add('editing');
        // preventScroll keeps the page from jumping to the codeblock when
        // focusing the editable on mobile — the browser would otherwise
        // scroll it into view above the keyboard, which reads as "auto-
        // scroll triggered by clicking Edit".
        try { codeEl.focus({ preventScroll: true }); } catch (_) { codeEl.focus(); }
        // Swap icon to checkmark
        btn.innerHTML = uiIcon("check", 14, {"role":"success"});
        btn.title = 'Done editing';
        btn.classList.add('active');
      }
    });

    // Tapping a code block body (not its buttons) toggles the overlay
    // copy/edit/run buttons, which otherwise cover the text on mobile.
    document.addEventListener('click', (e) => {
      if (e.target.closest('.copy-code, .edit-code, .run-code')) return;
      const pre = e.target.closest('pre');
      if (!pre || !pre.querySelector('.copy-code')) return;
      // Don't hide while editing — the buttons (incl. the Done checkmark) matter.
      if (pre.classList.contains('editing')) return;
      pre.classList.toggle('buttons-hidden');
    });

    // Position copy/run buttons top or bottom based on viewport position
    // — DESKTOP ONLY. On mobile this was constantly retriggering on tap
    // (synthetic mouseenter) and made the buttons jump, so the user's
    // finger landed on the moved target. Keep them pinned at the top on
    // touch — no auto-repositioning.
    document.addEventListener('mouseenter', (e) => {
      if (window.matchMedia('(max-width: 768px)').matches) return;
      const pre = e.target.closest ? e.target.closest('pre') : null;
      if (!pre || pre.dataset.btnPosComputed) return;
      const rect = pre.getBoundingClientRect();
      const threshold = window.innerHeight * 0.35;
      const isBottom = rect.top < threshold;
      const copyBtn = pre.querySelector('.copy-code');
      if (copyBtn) copyBtn.classList.toggle('bottom', isBottom);
      const editBtn = pre.querySelector('.edit-code');
      if (editBtn) editBtn.classList.toggle('bottom', isBottom);
      const runBtn = pre.querySelector('.run-code');
      if (runBtn) runBtn.classList.toggle('bottom', isBottom);
      pre.dataset.btnPosComputed = '1';
    }, true);

    // Tab suspension recovery: when user tabs back in, check if stream froze
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      const active = _getForegroundStreamState();
      if (!active) return;

      // Stream claims to be running — check if reader is actually alive
      const staleSince = Date.now() - (active.lastActivity || _lastReaderActivity);
      if (staleSince < 20000) return; // Active recently, probably fine

      // Reader hasn't produced data in 5+ seconds after tab resume.
      // Give it a short grace period then recover.
      console.warn('[tab-recovery] Stream appears frozen (no activity for ' + Math.round(staleSince/1000) + 's). Recovering...');

      setTimeout(() => {
        // Re-check — maybe the reader woke up during the grace period
        const stillActive = _getForegroundStreamState();
        if (!stillActive) return;
        const stillStale = Date.now() - (stillActive.lastActivity || _lastReaderActivity);
        if (stillStale < 5000) return; // Came back to life

        console.warn('[tab-recovery] Stream confirmed dead. Aborting and reloading session.');

        // Abort the frozen stream, but preserve the visible bubble.
        if (stillActive.abortCtrl) {
          stillActive.abortCtrl._reason = 'recovery';
          stillActive.abortCtrl.abort();
        }
        try {
          const sid = sessionModule && sessionModule.getCurrentSessionId && sessionModule.getCurrentSessionId();
          if (sid) _activeStreams.delete(sid);
        } catch (_) {}
        _syncForegroundStreamGlobals();

        // Release Web Lock
        if (_webLockRelease) {
          _webLockRelease();
          _webLockRelease = null;
        }

        // Reset UI state
        var _submitBtn = document.getElementById('submit');
        updateSubmitButton('idle', _submitBtn);
        var _msgInput = document.getElementById('message');
        if (_msgInput) _msgInput.disabled = false;
      }, 2000); // 2 second grace period
    });

    // On mobile, fade out welcome text when keyboard opens to prevent overlap
    if (window.innerWidth <= 768) {
      const msgInput = document.getElementById('message');
      if (msgInput) {
        msgInput.addEventListener('focus', () => {
          const ws = document.getElementById('welcome-screen');
          if (ws && !ws.classList.contains('hidden')) {
            ws.classList.add('kb-hidden');
          }
        });
        msgInput.addEventListener('blur', () => {
          const ws = document.getElementById('welcome-screen');
          if (ws && !ws.classList.contains('hidden')) {
            // Delay re-show so tapping within chatbox doesn't flash
            setTimeout(() => {
              if (document.activeElement !== msgInput) {
                ws.classList.remove('kb-hidden');
              }
            }, 200);
          }
        });
      }
      // Smooth viewport resize when keyboard opens/closes
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', () => {
          document.documentElement.style.setProperty('--vh', window.visualViewport.height + 'px');
        });
        document.documentElement.style.setProperty('--vh', window.visualViewport.height + 'px');
      }
    }

    // If the browser discarded and restored this tab, reload the current session
    // so the user sees the server-saved partial response instead of a blank page
    if (document.wasDiscarded) {
      console.warn('[tab-recovery] Tab was discarded by browser — reloading session');
      setTimeout(() => {
        var _sid = sessionModule && sessionModule.getCurrentSessionId();
        if (_sid) sessionModule.selectSession(_sid);
      }, 500);
    }
  }

  /**
   * Regenerate response: truncate history to the user message before this AI message,
   * then re-submit that user message.
   */
  /**
   * Edit a user message: show an input, truncate to before it, resubmit the edited text.
   */
  export async function editUserMessage(userMsgElement) {
    const box = document.getElementById('chat-history');
    const allMsgs = Array.from(box.querySelectorAll('.msg'));
    const msgIndex = allMsgs.indexOf(userMsgElement);
    if (msgIndex < 0) return;

    const bodyEl = userMsgElement.querySelector('.body');
    if (!bodyEl) return;
    const editorSession=sessionModule.getCurrentSessionId(), editorOwner=_inputOwner,
      editorAuthEpoch=_codeEvidenceAuthEpoch, editorNav=sessionModule.getSessionNavigationToken();
    const attachments=_collectMessageAttachments(userMsgElement);
    const currentText = (typeof userMsgElement.dataset.raw==='string' ? userMsgElement.dataset.raw : bodyEl.textContent || '').replace(/\s*\[\d+ attachment\(s\)\]$/, '');

    // Replace body with an editable textarea
    const editor = document.createElement('textarea');
    editor.className = 'edit-textarea';
    editor.value = currentText;
    editor.rows = Math.max(2, currentText.split('\n').length);

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex; gap:6px; margin-top:4px;';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'edit-save-btn';
    saveBtn.textContent = 'Send';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'edit-cancel-btn';
    cancelBtn.textContent = 'Cancel';
    btnRow.appendChild(saveBtn);
    btnRow.appendChild(cancelBtn);

    const originalHTML = bodyEl.innerHTML;
    bodyEl.innerHTML = '';
    bodyEl.appendChild(editor);
    bodyEl.appendChild(btnRow);
    editor.focus();

    cancelBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      bodyEl.innerHTML = originalHTML;
    });

    saveBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const newText = editor.value;
      if (!newText.trim() && !attachments.length) return;
      if (!editorSession || sessionModule.getCurrentSessionId()!==editorSession || _inputOwner!==editorOwner ||
          _codeEvidenceAuthEpoch!==editorAuthEpoch || sessionModule.getSessionNavigationToken()!==editorNav) return;
      saveBtn.disabled=true; editor.disabled=true;
      try {
        await _submitReplacement({sessionId:editorSession,sourceNodes:[userMsgElement],rows:allMsgs,keepCount:msgIndex,
          text:newText,attachments,options:null,variants:null,hideBubble:false,
          sourceCurrent:()=>editor.isConnected && editor.value===newText,
          commit:()=>{for(let i=allMsgs.length-1;i>=msgIndex;i--) allMsgs[i].remove();}});
      } finally {
        saveBtn.disabled=false; editor.disabled=false;
      }
    });

    // Also submit on Enter (without shift)
    editor.addEventListener('keydown', (e) => {
      const isMobile = window.innerWidth <= 768

      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !isMobile) {
        e.preventDefault();
        saveBtn.click();
      }
    });
  }

  /**
   * Resend a user message. Normal resend appends a fresh copy at the end of
   * the current thread; regenerate flows can opt into replacing from here.
   */
  function _collectMessageAttachments(element) {
    const ids=Array.from(element.querySelectorAll('[data-file-id]')).map(el=>el.dataset.fileId).filter(Boolean);
    if (!ids.length) for (const image of element.querySelectorAll('.attach-image-preview img, .attach-card img')) {
      const match=(image.getAttribute('src') || '').match(/\/api\/upload\/([A-Za-z0-9_\-]+)/);
      if (match && !ids.includes(match[1])) ids.push(match[1]);
    }
    return ids;
  }
  let _replacementActive = null;
  async function _submitReplacement({ sessionId, sourceNodes, rows, keepCount, text, attachments, options, variants, hideBubble, commit, sourceCurrent=()=>true }) {
    if (_replacementActive || isStreaming || _sendInFlight || window.compareModule?.isActive() || _hideUserBubble || slashCommands.getSetupMode()) {
      uiModule?.showError?.('Wait for the current chat action to finish.'); return;
    }
    const owner=_inputOwner, authEpoch=_codeEvidenceAuthEpoch, nav=sessionModule.getSessionNavigationToken();
    const draft=captureComposerDraftState(), box=document.getElementById('chat-history');
    const sourceMarkup=sourceNodes.map(node=>node?.outerHTML), route=JSON.stringify(sessionModule.getCurrentRoute() ?? null);
    const privateMode=!!uiModule.el('incognito-toggle')?.checked;
    const sameScope=()=>!!owner && _inputOwner===owner && document.body?.dataset?.accountId===owner &&
      _codeEvidenceAuthEpoch===authEpoch && sessionModule.getCurrentSessionId()===sessionId && sessionModule.getSessionNavigationToken()===nav;
    const current=(ownSend=false)=>{
      const now=captureComposerDraftState(), currentRows=Array.from(box?.querySelectorAll('.msg') || []);
      return sameScope() && sourceCurrent() && !isStreaming && (ownSend || !_sendInFlight) && !window.compareModule?.isActive() && !slashCommands.getSetupMode() &&
        JSON.stringify(sessionModule.getCurrentRoute() ?? null)===route && !!uiModule.el('incognito-toggle')?.checked===privateMode &&
        sourceNodes.every((node,index)=>node?.isConnected && node.outerHTML===sourceMarkup[index]) &&
        currentRows.length===rows.length && currentRows.every((node,index)=>node===rows[index]) &&
        now.sid===draft.sid && now.epoch===draft.epoch && now.version===draft.version && now.text===draft.text &&
        now.attachments===draft.attachments && now.pending.length===draft.pending.length && now.pending.every((file,index)=>file===draft.pending[index]);
    };
    const reconcile=async()=>{
      if (sameScope() && !isStreaming && !_sendInFlight && !window.compareModule?.isActive()) {
        try { await sessionModule.selectSession(sessionId,{preserveComposer:true}); } catch (_) {}
      }
    };
    if (!current()) { uiModule?.showError?.('The chat or draft changed. Try again.'); return; }
    const reservation={}; _replacementActive=reservation;
    let handedOff=false, releaseAdmission;
    const admissionDone=new Promise(resolve=>{releaseAdmission=resolve});
    let released=false;
    const release=()=>{if(released) return;released=true;if(_replacementActive===reservation) _replacementActive=null;releaseAdmission();};
    try {
      await _thinkingSavePromise;
      if (_thinkingSaveError || !current()) { if (sameScope()) uiModule?.showError?.('The chat or Thinking setting changed. Try again.'); return; }
      if (options) {
        const scope=`${owner}\u0000${sessionId}\u0000${privateMode}`;
        const restored=restoreReasoningEffortSelection(_thinkingOptionModel,options);
        if (_thinkingScopeKey!==scope || !['selected','default'].includes(restored.status)) {
          uiModule?.showError?.('The original Thinking setting is unavailable for the current model. Choose a compatible route before retrying.'); return;
        }
      }
      if (keepCount !== null) {
        let response, receipt;
        const graphScope=keepCount===0 ? chatCodeBoundaryScope(sessionId,owner,privateMode) : null;
        try {
          response=await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/truncate${graphScope?'?graph_activity=true':''}`,{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keep_count:keepCount})});
          if (!response.ok) {
            if (response.status>=400 && response.status<500 && response.status!==408) {
              uiModule?.showError?.('The chat could not be replaced.'); return;
            }
            throw new Error('truncate outcome unknown');
          }
          receipt=await response.json();
          if (!receipt || typeof receipt!=='object' || Array.isArray(receipt) ||
              Object.keys(receipt).filter(key=>graphScope?key!=='activity_boundary':true).sort().join(',')!=='kept,status,truncated' || receipt.status!=='ok' ||
              receipt.kept!==keepCount || receipt.truncated!==true) throw new Error('truncate receipt unavailable');
        } catch (_) {
          await reconcile(); uiModule?.showError?.('The replacement outcome is unknown; the chat was refreshed when possible.'); return;
        }
        if (sameScope()) reportCodeContextBoundary(graphScope,receipt.activity_boundary);
        if (!current()) { await reconcile(); uiModule?.showError?.('The chat or draft changed; no replacement was sent.'); return; }
      }
      const attempt={text,attachments:[...attachments],options:options ? structuredClone(options) : null,variants,hideBubble,reservation,release,
        current,accepted:false,commit:()=>{if(!current(true)) return false;commit();return true;}};
      handedOff=true;
      void handleChatSubmit({preventDefault(){}},attempt).catch(error=>{
        if (attempt.ownsSend) {
          attempt.ownsSend=false; _sendInFlight=false; _syncForegroundStreamGlobals();
          const input=uiModule.el('message'); if (input) input.disabled=false;
          document.querySelector('.send-btn')?.classList.remove('send-pending');
        }
        console.error('Replacement send failed:',error);
        if (sameScope()) uiModule?.showError?.('The replacement could not be sent.');
      }).finally(async()=>{
        try { if (!attempt.accepted && keepCount!==null) await reconcile(); }
        finally { release(); }
      });
      await admissionDone;
    } finally { if(!handedOff) release(); }
  }

  export async function resendUserMessage(userMsgElement, opts = {}) {
    const replaceFromHere = Boolean(opts && opts.replaceFromHere);
    const box = document.getElementById('chat-history');
    const allMsgs = Array.from(box.querySelectorAll('.msg'));
    const msgIndex = allMsgs.indexOf(userMsgElement);
    if (msgIndex < 0) return;

    // Prefer dataset.raw (stripped original user text) over .body.textContent
    // — the latter slurps the rendered "View image description" collapsible
    // content too, which would then be sent back as the user's question and
    // the AI would reply to that gibberish instead of the actual prompt.
    const bodyEl = userMsgElement.querySelector('.body');
    let text = (userMsgElement.dataset.raw || (bodyEl ? bodyEl.textContent : '') || '').trim();
    text = text.replace(/\s*\[\d+ attachment\(s\)\]$/, '');

    // Collect file_ids attached to this user message so the resend re-carries
    // the photos / docs (and the chat handler picks up the user-edited OCR
    // text cached server-side under those file ids).
    const _ids = _collectMessageAttachments(userMsgElement);

    // Rescue: legacy bubbles may have stored the filename as the message
    // content (artifact of earlier broken resends). Don't re-send that as
    // the user prompt if we still have the file attached. Loosen the regex
    // to cover real-world camera/screenshot names with spaces, parens,
    // multi-dots: "Screen Shot 2026-05-28 at 4.05.32 PM.png", "IMG (1).JPG".
    if (text && _ids.length && /^[^\n\r]{1,200}\.(png|jpe?g|gif|webp|svg|bmp|heic|heif)$/i.test(text)) {
      text = '';
    }
    // Empty text + no attachments → tell the user instead of silently bailing.
    // The common case is a regen during a pre-upload race where the bubble
    // never had an `[data-file-id]` to scrape.
    if (!text && !_ids.length) {
      if (uiModule?.showError) uiModule.showError('Nothing to resend — message has no text and no attachments yet (try again after the upload finishes).');
      return;
    }

    const sessionId = sessionModule.getCurrentSessionId();
    if (!sessionId) return;

    await _submitReplacement({sessionId,sourceNodes:[userMsgElement],rows:allMsgs,keepCount:replaceFromHere ? msgIndex : null,
      text,attachments:_ids,options:null,variants:null,hideBubble:replaceFromHere,commit:()=>{
        if (!replaceFromHere) return;
        let sibling=userMsgElement.nextSibling;
        while (sibling) {const next=sibling.nextSibling;sibling.remove();sibling=next;}
      }});
  }

  export async function regenerateFrom(aiMsgElement) {
    const box = document.getElementById('chat-history');
    const allMsgs = Array.from(box.querySelectorAll('.msg'));
    const aiIndex = allMsgs.indexOf(aiMsgElement);
    if (aiIndex < 0) return;

    // Find the preceding user message
    let userIndex = -1;
    let userText = '';
    let userMsgEl = null;
    for (let i = aiIndex - 1; i >= 0; i--) {
      if (allMsgs[i].classList.contains('msg-user')) {
        userIndex = i;
        userMsgEl = allMsgs[i];
        // Prefer dataset.raw (set by addMessage with the stripped, original
        // user text) over the rendered body's textContent — the latter
        // pulls in the "View image description" collapsible content too,
        // duplicating the OCR text on regen.
        const bodyEl = userMsgEl.querySelector('.body');
        userText = (userMsgEl.dataset.raw || (bodyEl ? bodyEl.textContent : '') || '').trim();
        userText = userText.replace(/\s*\[\d+ attachment\(s\)\]$/, '');
        break;
      }
    }

    if (userIndex < 0) {
      if (uiModule) uiModule.showError('Could not find the user message to regenerate');
      return;
    }
    let originalOptions = null;
    try {
      const raw=userMsgEl?.dataset?.modelRequestOptions, value=raw && raw.length<=16384 ? JSON.parse(raw) : null;
      if (value && typeof value==='object' && !Array.isArray(value) && Object.keys(value).sort().join(',')==='catalog_revision,options,route_id' &&
          typeof value.route_id==='string' && value.route_id.length>0 && value.route_id.length<=256 &&
          Number.isSafeInteger(value.catalog_revision) && value.catalog_revision>0 && value.options && typeof value.options==='object' && !Array.isArray(value.options)) originalOptions=value;
    }
    catch (_) { originalOptions = null; }

    // Collect any file_ids attached to the original user message so the
    // regenerated send re-uses them. Without this the AI is regenerated on
    // text alone — photos (and the user-edited OCR text cached server-side
    // under that file_id) would be silently dropped.
    const _regenIds = userMsgEl ? _collectMessageAttachments(userMsgEl) : [];
    // Rescue: earlier-version regens (before the dataset.raw fix) stored the
    // photo's filename as the user-message content. On a follow-up regen,
    // that filename would be sent back as the literal user prompt, so the
    // AI thinks the question is "blue_night_preview.jpg" and replies "that's
    // an image file". If userText is just a bare image filename and we have
    // attachments, drop it so the OCR text (or the image bytes for vision
    // models) is what the model actually sees.
    if (userText && _regenIds.length &&
        /^[^\n\r]{1,200}\.(png|jpe?g|gif|webp|svg|bmp|heic|heif)$/i.test(userText.trim())) {
      userText = '';
    }

    // A photo-only message has empty user text — regen must still proceed,
    // because the attachments themselves are the message. Bail only if there
    // is no text AND no attachments to send.
    if (!userText && !_regenIds.length) {
      if (uiModule) uiModule.showError('Nothing to regenerate — the user message has no text and no attachments');
      return;
    }

    const sessionId = sessionModule.getCurrentSessionId();
    if (!sessionId) return;

    // Save current response as a variant
    const oldRaw = aiMsgElement.dataset.raw || aiMsgElement.querySelector('.body')?.textContent || '';
    const oldHtml = aiMsgElement.querySelector('.body')?.innerHTML || '';
    let variants = [];
    try { variants = JSON.parse(aiMsgElement.dataset.variants || '[]'); } catch(_) {}
    if (variants.length === 0) {
      // First regen — save the original as variant 0
      variants.push({ raw: oldRaw, html: oldHtml, label: 'original' });
    }

    const keepCount = userIndex;

    await _submitReplacement({sessionId,sourceNodes:[userMsgEl,aiMsgElement],rows:allMsgs,keepCount,
      text:userText,attachments:_regenIds,options:originalOptions,variants,hideBubble:true,commit:()=>{
        for (let i=allMsgs.length-1;i>aiIndex;i--) allMsgs[i].remove();
        aiMsgElement.remove();
      }});
  }

  // Pending variants from a regeneration — transferred to new streaming element
  // File-ids carried over from the original user message during a regen, so
  // photos / OCR overrides survive into the new send. Consumed once.
  let _pendingRegenAttachments = null;
  let _restoredAttachmentOwner = null;
  let _restoredAttachmentArray = null;
  let _composerDraftVersion = 0;
  let _composerSessionEpoch = 0;
  let _composerSessionId = sessionModule.getCurrentSessionId();
  document.addEventListener('input', (event) => {
    if (event.target?.id === 'message') _composerDraftVersion++;
  });
  document.addEventListener('odysseus:session-selected', () => {
    const sid = sessionModule.getCurrentSessionId();
    if (sid !== _composerSessionId) {
      _composerSessionEpoch++;
      _composerSessionId = sid;
      if (_restoredAttachmentOwner !== sid && _pendingRegenAttachments === _restoredAttachmentArray) _pendingRegenAttachments = null;
      _restoredAttachmentOwner = null;
      _restoredAttachmentArray = null;
    }
  });
  function captureComposerDraftState() {
    return { sid: sessionModule.getCurrentSessionId(), epoch: _composerSessionEpoch, version: _composerDraftVersion,
      text: document.getElementById('message')?.value ?? '', attachments: _pendingRegenAttachments,
      pending: [...fileHandlerModule.getPendingRaw()] };
  }
  let _codeEvidenceAuthEpoch = 0;
  document.addEventListener('openclank:auth-context-changed', () => { _codeEvidenceAuthEpoch++; });
  function captureCodeEvidenceDraft() {
    const draft=captureComposerDraftState();
    const owner=_inputOwner && _inputOwner===document.body?.dataset?.accountId ? _inputOwner : '';
    return {...draft,owner,authEpoch:_codeEvidenceAuthEpoch};
  }
  fileHandlerModule.setDraftScopeCapture(() => {
    const draft = captureCodeEvidenceDraft();
    const nav = sessionModule.getSessionNavigationToken();
    const workspace = Storage.get(Storage.KEYS.WORKSPACE_ID, '') || '';
    const privateMode = !!uiModule.el('incognito-toggle')?.checked;
    const compare = !!window.compareModule?.isActive();
    return () => {
      const now = captureCodeEvidenceDraft();
      return draft.sid === now.sid && draft.epoch === now.epoch && draft.owner === now.owner
        && draft.authEpoch === now.authEpoch && draft.version === now.version && draft.text === now.text
        && draft.attachments === now.attachments && nav === sessionModule.getSessionNavigationToken()
        && workspace === (Storage.get(Storage.KEYS.WORKSPACE_ID, '') || '')
        && privateMode === !!uiModule.el('incognito-toggle')?.checked && compare === !!window.compareModule?.isActive();
    };
  });
  const invalidateFileAdditions = fileHandlerModule.invalidateFileAdditions;
  document.addEventListener('odysseus:session-selected', invalidateFileAdditions);
  document.addEventListener('openclank:auth-context-changed', invalidateFileAdditions);
  window.addEventListener('openclank:auth-context-changed', invalidateFileAdditions);
  window.addEventListener('workspace-change', invalidateFileAdditions);
  window.addEventListener('chat-workspace-change', invalidateFileAdditions);
  window.addEventListener('popstate', invalidateFileAdditions);
  window.addEventListener('pagehide', invalidateFileAdditions);
  uiModule.el('incognito-toggle')?.addEventListener('change', invalidateFileAdditions);
  document.addEventListener('visibilitychange', () => { if (document.hidden) invalidateFileAdditions(); });
  const invalidateOutsideChat = event => {
    if (fileHandlerModule.isOtherApplet(event.detail?.modal?.querySelector('.oc-applet-frame') || event.target)) {
      invalidateFileAdditions();
    }
  };
  window.addEventListener('odysseus:modal-opened', invalidateOutsideChat);
  window.addEventListener('odysseus:modal-restored', invalidateOutsideChat);
  document.addEventListener('focusin', invalidateOutsideChat, true);
  document.addEventListener('pointerdown', invalidateOutsideChat, true);
  fileHandlerModule.setPendingImageEditor(file => {
    const captured=captureCodeEvidenceDraft();
    captured.workspace=String(sessionModule.getSessions().find(row=>row.id===captured.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default');
    if (!captured.sid || !captured.owner || document.body.dataset.accountId!==captured.owner || !captured.pending.includes(file)) throw new Error('The attachment owner is unavailable.');
    const scopeCurrent=() => { const now=captureCodeEvidenceDraft(), workspace=String(sessionModule.getSessions().find(row=>row.id===now.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default'); return now.sid===captured.sid && now.epoch===captured.epoch && now.authEpoch===captured.authEpoch && now.owner===captured.owner && workspace===captured.workspace && document.body.dataset.accountId===captured.owner; };
    const isCurrent=() => { const now=captureCodeEvidenceDraft(); return scopeCurrent() && now.version===captured.version && now.text===captured.text && now.attachments===captured.attachments && now.pending.length===captured.pending.length && now.pending.every((item,index)=>item===captured.pending[index]); };
    return beginAttachmentEdit({file,capture:captured,isCurrent,open:openPendingAttachment,commit:(original,blob,receipt)=>fileHandlerModule.replacePendingWithManaged(original,blob,receipt,isCurrent,scopeCurrent)});
  });
  fileHandlerModule.setDerivedImageAttacher(async descriptor => {
    const captured=captureCodeEvidenceDraft(), workspace=String(sessionModule.getSessions().find(row=>row.id===captured.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default');
    if (descriptor?.state!=='committed' || !captured.sid || captured.owner!==descriptor.owner || workspace!==descriptor.workspace || document.body.dataset.accountId!==captured.owner) throw new Error('The result belongs to a different account or workspace.');
    const scopeCurrent=() => { const now=captureCodeEvidenceDraft(), ws=String(sessionModule.getSessions().find(row=>row.id===now.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default'); return now.sid===captured.sid && now.epoch===captured.epoch && now.authEpoch===captured.authEpoch && now.owner===captured.owner && ws===workspace && document.body.dataset.accountId===captured.owner; };
    const isCurrent=() => { const now=captureCodeEvidenceDraft(); return scopeCurrent() && now.version===captured.version && now.text===captured.text && now.attachments===captured.attachments && now.pending.length===captured.pending.length && now.pending.every((item,index)=>item===captured.pending[index]); };
    const response=await fetch(`${API_BASE}/api/upload/${encodeURIComponent(descriptor.uploadId)}`,{credentials:'same-origin'}); if (!response.ok || !isCurrent()) throw new Error('The saved result or chat is unavailable.');
    const blob=await response.blob(), digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),byte=>byte.toString(16).padStart(2,'0')).join('');
    if (digest!==descriptor.pngDigest || !isCurrent() || !fileHandlerModule.addManagedPending(blob,{id:descriptor.uploadId,checksum_sha256:digest,sessionId:captured.sid},isCurrent,scopeCurrent)) throw new Error('The result or chat changed before attachment.');
    uiModule.showToast?.('Image attached to the current draft.');
  });
  async function openAuthorizedImageSource(kind,id,name,sourceCurrent=()=>true,sourceChatId=null) {
    if (!['upload','generated'].includes(kind) || typeof id!=='string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(id) || id==='.' || id==='..' || typeof sourceCurrent!=='function' || !sourceCurrent()) throw new Error('The image source is unavailable.');
    const captured=captureCodeEvidenceDraft(); captured.workspace=String(sessionModule.getSessions().find(row=>row.id===captured.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default');
    if (!captured.sid || !captured.owner || document.body.dataset.accountId!==captured.owner) throw new Error('The image owner is unavailable.');
    const scopeCurrent=() => {const now=captureCodeEvidenceDraft(), workspace=String(sessionModule.getSessions().find(row=>row.id===now.sid)?.workspace_id || Storage.get(Storage.KEYS.WORKSPACE_ID,'') || 'default'); return now.sid===captured.sid && now.epoch===captured.epoch && now.authEpoch===captured.authEpoch && now.owner===captured.owner && workspace===captured.workspace && document.body.dataset.accountId===captured.owner;};
    const isCurrent=() => {const now=captureCodeEvidenceDraft();return sourceCurrent() && scopeCurrent() && now.version===captured.version && now.text===captured.text && now.attachments===captured.attachments && now.pending.length===captured.pending.length && now.pending.every((item,index)=>item===captured.pending[index]);};
    const locator=`/api/${kind==='upload' ? 'upload' : 'generated-image'}/${encodeURIComponent(id)}`;
    const response=await fetch(`${API_BASE}${locator}`,{credentials:'same-origin'}); if (!response.ok || !isCurrent()) throw new Error('The authorized image is unavailable or its origin changed.');
    const blob=await response.blob(); if (!isCurrent() || blob.size<1 || blob.size>32*1024*1024 || !/^image\//i.test(blob.type)) throw new Error('The image is unavailable or exceeds the import limit.');
    const file=new File([blob],String(name || 'Image').slice(0,200),{type:blob.type}); Object.assign(captured,{sourceKind:kind,sourceId:id,sourceLocator:locator,sourceChatId});
    return beginAttachmentEdit({file,capture:captured,isCurrent,open:openPendingAttachment,commit:(_original,edited,receipt)=>fileHandlerModule.addManagedPending(edited,receipt,isCurrent,scopeCurrent)});
  }
  function appendCodeEvidence(evidence, {expected,isCurrent,ask=false}={}) {
    if(!document.getElementById('message'))return {ok:false,message:'Chat composer unavailable.'};
    return appendCodeEvidenceCAS(evidence,{expected,isCurrent,ask,read:captureCodeEvidenceDraft,
      commit:text=>{const input=document.getElementById('message');input.value=text;input.dispatchEvent(new Event('input',{bubbles:true}));},
      submit:()=>{document.querySelector('.send-btn')?.click();}});
  }
  function attachGeneratedSummary(receipt,{expected,isCurrent=()=>true}={}) {
    return stageGeneratedSummaryCAS(receipt,{expected,read:captureCodeEvidenceDraft,account:()=>document.body.dataset.accountId,isCurrent,enqueue:fileHandlerModule.addManagedSummaryPending})
  }
  function restoreComposerDraft(receipt, { sid, expected } = {}) {
    if (!document.getElementById('message')) return { restored: false, reason: 'composer-unavailable' };
    return restoreValidatedComposerDraft(receipt, { sid, expected, read: captureComposerDraftState, commit: ({ text, attachmentIds }) => {
      const input = document.getElementById('message');
      input.value = text;
      fileHandlerModule.clearPending();
      _pendingRegenAttachments = attachmentIds;
      _restoredAttachmentOwner = sid;
      _restoredAttachmentArray = attachmentIds;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } });
  }

  /**
   * Called after streaming completes to attach variant navigation if this was a regen.
   */
  function _attachVariantNav(msgElement, variants) {
    if (!variants) return;

    // Add the new response as the latest variant
    const newRaw = msgElement.dataset.raw || msgElement.querySelector('.body')?.textContent || '';
    const newHtml = msgElement.querySelector('.body')?.innerHTML || '';
    const varLabel = 'regen';
    variants.push({ raw: newRaw, html: newHtml, label: varLabel });

    msgElement.dataset.variants = JSON.stringify(variants);
    msgElement.dataset.variantIndex = String(variants.length - 1);

    _renderVariantNav(msgElement, variants, variants.length - 1);

    // Persist variants to server
    const sid = sessionModule.getCurrentSessionId();
    if (sid) {
      fetch(`${API_BASE}/api/session/${sid}/update-last-meta`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ metadata: { variants: variants, variantIndex: variants.length - 1 } })
      }).catch(e => console.warn('update-last-meta (variants) failed:', e));
    }
  }

  const _VARIANT_ICONS = { regen: '\u21BB', shorter: '\u2702', simpler: '?', original: '\u25CB' };
  function _variantTagText(label) {
    return _VARIANT_ICONS[label] || _VARIANT_ICONS['original'];
  }

  function _renderVariantNav(msgElement, variants, currentIdx) {
    // Remove existing nav if any
    const old = msgElement.querySelector('.variant-nav');
    if (old) old.remove();

    if (variants.length < 2) return;

    const nav = document.createElement('span');
    nav.className = 'variant-nav';
    nav.addEventListener('click', (e) => e.stopPropagation());

    // Label showing what this variant is
    // Divider
    const divider = document.createElement('span');
    divider.className = 'variant-divider';
    divider.textContent = '|';
    nav.appendChild(divider);

    // Label
    const curVariant = variants[currentIdx];
    const tagLabel = document.createElement('span');
    tagLabel.className = 'variant-tag' + (curVariant?.label === 'shorter' ? ' variant-tag-scissors' : '');
    tagLabel.textContent = _variantTagText(curVariant?.label);
    nav.appendChild(tagLabel);

    // < button
    const prevBtn = document.createElement('button');
    prevBtn.className = 'variant-btn';
    prevBtn.textContent = '<';
    prevBtn.disabled = currentIdx === 0;
    prevBtn.addEventListener('click', (e) => { e.stopPropagation(); _switchVariant(msgElement, variants, currentIdx - 1); });
    nav.appendChild(prevBtn);

    // Clickable number for current index (click left number = go left, right = go right)
    const numLeft = document.createElement('button');
    numLeft.className = 'variant-num';
    numLeft.textContent = String(currentIdx + 1);
    numLeft.disabled = currentIdx === 0;
    numLeft.addEventListener('click', (e) => { e.stopPropagation(); _switchVariant(msgElement, variants, currentIdx - 1); });
    nav.appendChild(numLeft);

    const slash = document.createElement('span');
    slash.className = 'variant-slash';
    slash.textContent = '/';
    nav.appendChild(slash);

    const numRight = document.createElement('button');
    numRight.className = 'variant-num';
    numRight.textContent = String(variants.length);
    numRight.disabled = currentIdx === variants.length - 1;
    numRight.addEventListener('click', (e) => { e.stopPropagation(); _switchVariant(msgElement, variants, currentIdx + 1); });
    nav.appendChild(numRight);

    // > button
    const nextBtn = document.createElement('button');
    nextBtn.className = 'variant-btn';
    nextBtn.textContent = '>';
    nextBtn.disabled = currentIdx === variants.length - 1;
    nextBtn.addEventListener('click', (e) => { e.stopPropagation(); _switchVariant(msgElement, variants, currentIdx + 1); });
    nav.appendChild(nextBtn);

    // Insert into the .role header
    const roleEl = msgElement.querySelector('.role');
    if (roleEl) {
      roleEl.appendChild(nav);
    } else {
      msgElement.appendChild(nav);
    }
  }

  function _switchVariant(msgElement, variants, newIdx) {
    if (newIdx < 0 || newIdx >= variants.length) return;
    const v = variants[newIdx];
    const body = msgElement.querySelector('.body');
    if (body) body.innerHTML = v.html;
    msgElement.dataset.raw = v.raw;
    msgElement.dataset.variantIndex = String(newIdx);
    if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(msgElement);
    _renderVariantNav(msgElement, variants, newIdx);

    // Persist selected variant to server
    const sid = sessionModule.getCurrentSessionId();
    if (sid) {
      fetch(`${API_BASE}/api/session/${sid}/update-last-meta`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ metadata: { variantIndex: newIdx } })
      }).catch(e => console.warn('update-last-meta (variantIndex) failed:', e));
    }
  }

  export async function forkFrom(messageElement) {
    if (!messageElement.isConnected || !messageElement.closest('#chat-history')) return;
    await forkConversation({
      sid: sessionModule.getCurrentSessionId(),
      selection: {
        id: messageElement.dataset.dbId,
        boundary: messageElement.classList.contains('msg-user') ? 'before' : 'through'
      }
    });
  }

  /**
   * Check for pending/completed research after page refresh or session switch.
   * If research is still running, show a spinner and poll until done.
   * If research is done, fetch result and render it.
   */
  export async function checkPendingResearch(sessionId) {
    if (!sessionId) return;
    const researchScope = captureContinuityScope();
    try {
      const res = await fetch(`${API_BASE}/api/research/status/${sessionId}`);
      if (!res.ok) {
        if (sessionModule && sessionModule.clearResearching) sessionModule.clearResearching(sessionId);
        return; // 404 = no research for this session
      }
      const data = await res.json();

      if (data.status === 'done') {
        // Fetch and render the completed result
        _notifyResearchComplete(sessionId, data.query || '', researchScope);
        if (sessionModule && sessionModule.clearResearching) sessionModule.clearResearching(sessionId);
        const resultRes = await fetch(`${API_BASE}/api/research/result/${sessionId}`, { method: 'POST' });
        if (resultRes.ok) {
          const resultData = await resultRes.json();
          if (resultData.result) {
            // Skip if history already has a research message for this session
            if (document.querySelector(`#chat-history .msg-ai[data-research-session="${sessionId}"]`)) return;

            var srcBox = '';
            if (resultData.sources && resultData.sources.length > 0) {
              srcBox = _buildSourcesBox(resultData.sources, 'research');
            }
            var findingsBox = chatRenderer.buildFindingsBox(resultData.raw_findings);
            var cleanResult = resultData.result;
            // Build DOM directly to avoid double-processing through addMessage
            chatRenderer.hideWelcomeScreen();
            var _box = document.getElementById('chat-history');
            if (_box) {
              var _wrap = document.createElement('div');
              _wrap.className = 'msg msg-ai';
              _wrap.dataset.researchSession = sessionId;
              var _role = document.createElement('div');
              _role.className = 'role';
              var _meta = sessionModule.getSessions().find(function(s) { return s.id === sessionId; });
              _role.textContent = _shortModel(_meta?.model);
              _applyModelColor(_role, _meta?.model);
              _role.appendChild(chatRenderer.roleTimestamp());
              var _body = document.createElement('div');
              _body.className = 'body';
              _body.innerHTML = srcBox + markdownModule.processWithThinking(
                markdownModule.squashOutsideCode(cleanResult)
              ) + findingsBox;
              _wrap.dataset.raw = cleanResult;
              _wrap.appendChild(_role);
              _wrap.appendChild(_body);
              _wrap.appendChild(chatRenderer.createMsgFooter(_wrap));
              _appendViewReportLink(_wrap, sessionId);
              _box.appendChild(_wrap);
              if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(_wrap);
              uiModule.scrollHistory();
            }
          }
        }
        return;
      }

      if (data.status !== 'running') return;

      // Don't show reconnect UI if we've already switched away
      if (sessionModule.getCurrentSessionId() !== sessionId) return;

      // Research is still running — show reconnect UI with spinner
      const box = document.getElementById('chat-history');
      if (!box) return;

      const holder = document.createElement('div');
      holder.className = 'msg msg-ai research-reconnect';
      holder.dataset.researchSession = sessionId;
      const roleTs = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
      const agentMeta = sessionModule.getSessions().find(s => s.id === sessionModule.getCurrentSessionId());
      const agentModelLabel = _shortModel(agentMeta?.model);
      holder.innerHTML = `<div class="role">${uiModule.esc(agentModelLabel)} <span class="role-timestamp">${roleTs}</span></div><div class="body"></div>`;
      _applyModelColor(holder.querySelector('.role'), agentMeta?.model);
      box.appendChild(holder);

      const bodyDiv = holder.querySelector('.body');
      const spinner = spinnerModule.create('Reconnecting to research...', 'right');
      bodyDiv.appendChild(spinner.createElement());
      spinner.start();

      // Update spinner with current progress if available
      function updateSpinnerFromProgress(progress) {
        if (!progress || !progress.phase) return;
        const rp = progress;
        if (rp.phase === 'probing') {
          spinner.updateMessage(`Verifying model: ${rp.model || '?'}`);
        } else if (rp.phase === 'planning') {
          spinner.updateMessage('Analyzing question & planning research strategy');
        } else if (rp.phase === 'searching') {
          const q = rp.queries ? `${rp.queries} queries` : '';
          const s = rp.total_sources ? ` · ${rp.total_sources} sources` : '';
          spinner.updateMessage(`Round ${rp.round || '?'}: Searching${q ? ' (' + q + ')' : ''}${s}`);
        } else if (rp.phase === 'reading') {
          spinner.updateMessage(rp.title ? `Reading: ${rp.title}` : `Round ${rp.round || '?'}: Reading ${rp.new_sources || ''} pages · ${rp.total_sources || 0} sources total`);
        } else if (rp.phase === 'analyzing') {
          spinner.updateMessage(`Round ${rp.round || '?'}: Analyzing ${rp.total_findings || 0} findings`);
        } else if (rp.phase === 'writing') {
          spinner.updateMessage(`Writing report · ${rp.total_sources || 0} sources`);
        }
      }

      updateSpinnerFromProgress(data.progress);
      _researchingStreamIds.add(sessionId);
      if (sessionModule && sessionModule.markResearching) sessionModule.markResearching(sessionId);

      // Restore research timer from started_at
      if (data.started_at && spinner && spinner.element) {
        _researchStartTime = data.started_at * 1000;
        _researchAvgDuration = data.avg_duration || null;
        _researchTimerEl = document.createElement('div');
        _researchTimerEl.className = 'research-timer';
        _researchTimerEl.style.cssText = 'font-size:0.8em; opacity:0.6; margin-top:4px; font-family:monospace;';
        spinner.element.parentNode.insertBefore(_researchTimerEl, spinner.element.nextSibling);
        _researchTimerInterval = setInterval(() => {
          if (!_researchTimerEl) return;
          var elapsed = Math.floor((Date.now() - _researchStartTime) / 1000);
          var mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
          var ss = String(elapsed % 60).padStart(2, '0');
          var txt = mm + ':' + ss;
          if (_researchAvgDuration) {
            var avgM = String(Math.floor(_researchAvgDuration / 60)).padStart(2, '0');
            var avgS = String(Math.round(_researchAvgDuration % 60)).padStart(2, '0');
            txt += ' / avg ' + avgM + ':' + avgS;
          }
          _researchTimerEl.textContent = txt;
        }, 1000);
        // Reconnect synapse — seed it with whatever progress is already known
        try {
          _researchSynapse = createResearchSynapse(spinner.element.parentNode, {
            query: data.query || '',
            startedAt: _researchStartTime,
          });
          if (_researchSynapse.element && _researchTimerEl) {
            spinner.element.parentNode.insertBefore(_researchSynapse.element, _researchTimerEl);
          }
          if (data.progress) {
            _researchSynapse.setPhase(data.progress.phase, data.progress);
            if (typeof data.progress.round === 'number') _researchSynapse.setRound(data.progress.round);
            if (typeof data.progress.total_sources === 'number') _researchSynapse.setSourceCount(data.progress.total_sources);
          }
        } catch (e) { console.warn('synapse reconnect failed', e); }
      }

      // Poll for completion
      const pollInterval = setInterval(async () => {
        // Stop polling if user switched to a different session
        if (sessionModule.getCurrentSessionId() !== sessionId) {
          clearInterval(pollInterval);
          spinner.destroy();
          _clearResearchTimer();
          if (holder.parentNode) holder.remove();
          _researchingStreamIds.delete(sessionId);
          if (_researchingStreamIds.size === 0) {
            var _rToggleP = document.getElementById('research-toggle-btn');
            if (_rToggleP) _rToggleP.classList.remove('research-running');
          }
          return;
        }
        try {
          const pollRes = await fetch(`${API_BASE}/api/research/status/${sessionId}`);
          if (!pollRes.ok) {
            clearInterval(pollInterval);
            spinner.destroy();
            _clearResearchTimer();
            _researchingStreamIds.delete(sessionId);
            if (sessionModule && sessionModule.clearResearching) sessionModule.clearResearching(sessionId);
            return;
          }
          const pollData = await pollRes.json();
          updateSpinnerFromProgress(pollData.progress);
          if (_researchSynapse && pollData.progress) {
            _researchSynapse.setPhase(pollData.progress.phase, pollData.progress);
            if (typeof pollData.progress.round === 'number') _researchSynapse.setRound(pollData.progress.round);
            if (typeof pollData.progress.total_sources === 'number') _researchSynapse.setSourceCount(pollData.progress.total_sources);
          }

          if (pollData.status !== 'running') {
            clearInterval(pollInterval);
            spinner.destroy();
            _clearResearchTimer();
            _researchingStreamIds.delete(sessionId);
            if (sessionModule && sessionModule.clearResearching) sessionModule.clearResearching(sessionId);

            if (pollData.status === 'done') {
              _notifyResearchComplete(sessionId, data.query || '', researchScope);
              const rRes = await fetch(`${API_BASE}/api/research/result/${sessionId}`, { method: 'POST' });
              if (rRes.ok) {
                const rData = await rRes.json();
                if (rData.result) {
                  var srcHtml = '';
                  if (rData.sources && rData.sources.length > 0) {
                    srcHtml = _buildSourcesBox(rData.sources, 'research');
                  }
                  var findingsHtml = chatRenderer.buildFindingsBox(rData.raw_findings);
                  bodyDiv.innerHTML = srcHtml + markdownModule.processWithThinking(
                    markdownModule.squashOutsideCode(rData.result)
                  ) + findingsHtml;
                  holder.dataset.raw = rData.result;
                  _appendViewReportLink(holder, sessionId);
                  if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(holder);
                }
              }
            } else {
              bodyDiv.innerHTML = '<i style="color: var(--color-error);">[Research ' + pollData.status + ']</i>';
            }
          }
        } catch (e) {
          console.error('Research poll error:', e);
        }
      }, 2000);
    } catch (e) {
      // No research pending, that's fine
    }
  }

  /** Set a display override for the next user message bubble */
  export function setDisplayOverride(text) {
    _displayOverride = text;
  }

  /** Hide the user bubble for the next submit (e.g. continue after stop) */
  export function setHideUserBubble() {
    _hideUserBubble = true;
  }

  /** Set the AI element to merge with the next streamed response (continue after stop) */
  export function setPendingContinue(el) {
    _pendingContinue = el;
  }

  /**
   * Delete an AI message and its preceding user message from the conversation.
   */
  export async function deleteMessage(msgElement) {
    if (uiModule && uiModule.styledConfirm) {
      const ok = await uiModule.styledConfirm('Delete this message?', {
        confirmText: 'Delete',
        cancelText: 'Cancel',
        danger: true,
      });
      if (!ok) return;
    }

    const box = document.getElementById('chat-history');
    const allMsgs = Array.from(box.querySelectorAll('.msg'));
    const clickedIndex = allMsgs.indexOf(msgElement);
    if (clickedIndex < 0) return;

    // No early-out on a missing session: an output shown before any model was
    // selected (issue #1428) has no session/persisted rows, but its "x" must
    // still remove it. We only need the session id for the server-side delete
    // below; without one we fall back to removing the DOM.
    const sessionId = sessionModule.getCurrentSessionId();

    const clickedIsUser = msgElement.classList.contains('msg-user');

    // Find the user+AI pair
    let userIndex = -1;
    let aiIndex = -1;
    if (clickedIsUser) {
      userIndex = clickedIndex;
      // Find the following AI message
      for (let i = clickedIndex + 1; i < allMsgs.length; i++) {
        if (allMsgs[i].classList.contains('msg-ai') && !allMsgs[i].classList.contains('msg-continuation')) {
          aiIndex = i;
          break;
        }
        if (allMsgs[i].classList.contains('msg-user')) break; // next user msg, no AI response
      }
    } else {
      // If clicked on a continuation, walk back to the main AI message
      let mainAiIndex = clickedIndex;
      if (allMsgs[mainAiIndex].classList.contains('msg-continuation')) {
        for (let i = mainAiIndex - 1; i >= 0; i--) {
          if (allMsgs[i].classList.contains('msg-ai') && !allMsgs[i].classList.contains('msg-continuation')) {
            mainAiIndex = i;
            break;
          }
        }
      }
      aiIndex = mainAiIndex;
      // Find the preceding user message
      for (let i = aiIndex - 1; i >= 0; i--) {
        if (allMsgs[i].classList.contains('msg-user')) {
          userIndex = i;
          break;
        }
      }
    }

    // Collect DB message IDs and DOM elements to remove
    const msgIds = [];
    const domToRemove = [];

    // Add the user message if found
    if (userIndex >= 0) {
      domToRemove.push(allMsgs[userIndex]);
      const uid = allMsgs[userIndex].dataset.dbId;
      if (uid) msgIds.push(uid);
    }

    // Add the AI message if found
    if (aiIndex >= 0) {
      domToRemove.push(allMsgs[aiIndex]);
      const aid = allMsgs[aiIndex].dataset.dbId;
      if (aid) msgIds.push(aid);

      const aiEl = allMsgs[aiIndex];
      // Also remove agent-thread elements BETWEEN user and AI
      if (userIndex >= 0) {
        let between = allMsgs[userIndex].nextElementSibling;
        while (between && between !== aiEl) {
          domToRemove.push(between);
          between = between.nextElementSibling;
        }
      }
      // Walk forward from the AI element to remove continuations and tool bubbles
      let sibling = aiEl.nextElementSibling;
      while (sibling) {
        if (sibling.classList.contains('msg-user') ||
            (sibling.classList.contains('msg-ai') && !sibling.classList.contains('msg-continuation'))) {
          break;
        }
        domToRemove.push(sibling);
        sibling = sibling.nextElementSibling;
      }
    }

    if (!msgIds.length || !sessionId) {
      // No persisted rows to delete (no DB IDs, or no session at all — e.g. an
      // error output shown before a model was selected, #1428). Just remove the
      // DOM so the "x" works regardless.
      domToRemove.forEach(el => el.remove());
      if (uiModule) uiModule.showToast('Message deleted');
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/api/session/${sessionId}/delete-messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msg_ids: msgIds })
      });
      if (!res.ok) throw new Error('Server error ' + res.status);
      domToRemove.forEach(el => el.remove());
      if (uiModule) uiModule.showToast('Message deleted');
    } catch (err) {
      console.error('Delete failed:', err);
      if (uiModule) uiModule.showError('Delete failed: ' + err.message);
    }
  }

  /**
   * Edit an AI message inline. Makes the body contentEditable, saves to DB on confirm.
   */
  export async function editAIMessage(msgElement) {
    const body = msgElement.querySelector('.body');
    if (!body) return;

    const isEditing = body.contentEditable === 'true' || body.contentEditable === 'plaintext-only';
    if (isEditing) return; // already editing

    const originalRaw = msgElement.dataset.raw || body.textContent || '';

    // Create editable textarea overlay
    const textarea = document.createElement('textarea');
    textarea.className = 'msg-edit-textarea';
    textarea.value = originalRaw;
    textarea.style.width = '100%';
    textarea.style.minHeight = Math.max(100, body.offsetHeight) + 'px';
    body.style.display = 'none';
    body.parentNode.insertBefore(textarea, body.nextSibling);
    textarea.focus();

    // Add save/cancel bar
    const bar = document.createElement('div');
    bar.className = 'msg-edit-bar';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'msg-edit-save';
    saveBtn.textContent = 'Save';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'msg-edit-cancel';
    cancelBtn.textContent = 'Cancel';
    bar.appendChild(saveBtn);
    bar.appendChild(cancelBtn);
    textarea.parentNode.insertBefore(bar, textarea.nextSibling);

    function cleanup() {
      textarea.remove();
      bar.remove();
      body.style.display = '';
    }

    cancelBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      cleanup();
    });

    saveBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const newContent = textarea.value;
      if (newContent === originalRaw) { cleanup(); return; }

      const msgId = msgElement.dataset.dbId;
      if (!msgId) { if (uiModule) uiModule.showError('Cannot edit: message ID not found'); cleanup(); return; }

      const sessionId = sessionModule.getCurrentSessionId();
      if (!sessionId) { cleanup(); return; }

      try {
        const sourceRes = await fetch(`${API_BASE}/api/session/${sessionId}/message-source/${encodeURIComponent(msgId)}`);
        if (!sourceRes.ok) throw new Error('Could not check the current message');
        const source = await sourceRes.json();
        if (source.content !== originalRaw) {
          await sessionModule.selectSession(sessionId, { preserveComposer: true });
          throw new Error('Message changed; the conversation was refreshed');
        }
        const res = await fetch(`${API_BASE}/api/session/${sessionId}/edit-message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ msg_id: msgId, content: newContent, expected_revision: source.source_revision }),
        });
        if (res.status === 409) {
          await sessionModule.selectSession(sessionId, { preserveComposer: true });
          throw new Error('Message changed; the conversation was refreshed');
        }
        if (!res.ok) throw new Error('Server error ' + res.status);

        // Re-render body with markdown
        body.innerHTML = markdownModule.processWithThinking(markdownModule.squashOutsideCode(newContent));
        msgElement.dataset.raw = newContent;

        // Add edited indicator if not already present
        if (!msgElement.querySelector('.edited-indicator')) {
          const indicator = document.createElement('div');
          indicator.className = 'edited-indicator';
          indicator.textContent = '[Message edited]';
          body.parentNode.insertBefore(indicator, body.nextSibling);
        }

        cleanup();
        if (uiModule) uiModule.showToast('Message edited');
      } catch (err) {
        console.error('Edit failed:', err);
        if (uiModule) uiModule.showError('Edit failed: ' + err.message);
      }
    });
  }

  /**
   * Rewrite the AI's last response with a specific instruction.
   * Uses the lightweight /api/rewrite endpoint — no tools, no agent loop.
   * Just rewrites the text of the last AI bubble.
   */
  export async function rewriteWith(aiMsgElement, instruction) {
    const sessionId = sessionModule.getCurrentSessionId();
    if (!sessionId) return;
    const rewriteId = aiMsgElement.dataset.dbId || null;
    const rewriteNav = sessionModule.getSessionNavigationToken();

    // Get the original text from the AI bubble
    const oldRaw = aiMsgElement.dataset.raw || aiMsgElement.querySelector('.body')?.textContent || '';
    const oldHtml = aiMsgElement.querySelector('.body')?.innerHTML || '';

    if (!oldRaw.trim()) {
      if (uiModule) uiModule.showError('No text to rewrite');
      return;
    }

    // Save current response as a variant
    let variants = [];
    try { variants = JSON.parse(aiMsgElement.dataset.variants || '[]'); } catch(_) {}
    if (variants.length === 0) {
      variants.push({ raw: oldRaw, html: oldHtml, label: 'original' });
    }

    // Determine label from instruction
    let varLabel = 'rewrite';
    if (instruction.includes('shorter')) varLabel = 'shorter';
    else if (instruction.includes('simpler')) varLabel = 'simpler';

    // Clear the bubble and show a whirlpool spinner while we wait for the
    // rewrite (replaces the old "Rewriting..." text).
    const bodyEl = aiMsgElement.querySelector('.body');
    let _rwSpin = null;
    if (bodyEl) {
      bodyEl.innerHTML = '';
      _rwSpin = spinnerModule.createWhirlpool(18);
      _rwSpin.element.style.margin = '4px 0';
      bodyEl.appendChild(_rwSpin.element);
    }
    // Stop + detach the spinner (called once real content starts rendering, and
    // on the failure path so it never spins forever).
    const _killRwSpin = () => { if (_rwSpin) { try { _rwSpin.destroy(); } catch (_) {} _rwSpin = null; } };

    try {
      const res = await fetch(`${API_BASE}/api/rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          message_id: rewriteId,
          original_text: oldRaw,
          instruction: instruction,
        }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let newText = '';
      let rewriteCommitted = false;
      let rewriteReceipt = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') { rewriteCommitted = true; continue; }
          try {
            const data = JSON.parse(payload);
            // The endpoint streams `event: error\ndata: {error,status}` on
            // failure — surface it instead of silently hanging on "Rewriting…".
            if (data.error) {
              throw new Error(data.error || ('HTTP ' + (data.status || 500)));
            }
            if (data.rewrite_commit) {
              if (data.rewrite_commit.message_id !== rewriteId || !/^[0-9a-f]{64}$/.test(data.rewrite_commit.content_hash || '') || typeof data.rewrite_commit.content !== 'string') throw new Error('Rewrite receipt changed');
              rewriteReceipt = data.rewrite_commit;
              newText = data.rewrite_commit.content;
            }
            // Reasoning tokens (vLLM --reasoning-parser: Qwen3 / DeepSeek-R1)
            // arrive as separate {delta, thinking:true} chunks. They are NOT
            // the rewrite — fold them away so they don't pollute the result.
            if (data.thinking) continue;
            if (data.delta) {
              newText += data.delta;
              _killRwSpin();
              if (bodyEl) {
                bodyEl.innerHTML = markdownModule.processWithThinking(
                  markdownModule.squashOutsideCode(newText)
                );
              }
            }
          } catch (e) {
            if (e instanceof Error && e.message) throw e;  // re-throw real errors
            /* ignore JSON parse noise */
          }
        }
      }

      if (!rewriteCommitted) throw new Error('Rewrite was not committed');
      if (sessionModule.getCurrentSessionId() !== sessionId || sessionModule.getSessionNavigationToken() !== rewriteNav || !aiMsgElement.isConnected) return;

      // Strip any thinking markup from the answer. A reasoning model may emit
      // an inline <think>…</think> block, a bare </think> (no opener), or — when
      // its reasoning came via reasoning_content — a stray leading <think> that
      // never closes (so it would otherwise hide the whole answer). Peel all of
      // those off so what's left is just the rewritten text.
      const _stripThink = (t) => {
        t = markdownModule.normalizeThinkingMarkup(t || '');
        t = t.replace(/<(?:think(?:ing)?|thought)(?:\s+[^>]*)?>[\s\S]*?<\/(?:think(?:ing)?|thought)>/gi, '');   // complete blocks
        if (/<\/(?:think(?:ing)?|thought)>/i.test(t)) t = t.replace(/^[\s\S]*?<\/(?:think(?:ing)?|thought)>/i, '');  // reasoning w/o opener
        return t.replace(/<\/?(?:think(?:ing)?|thought)(?:\s+[^>]*)?>/gi, '').trim();        // any orphan tag
      };
      newText = rewriteReceipt ? rewriteReceipt.content : _stripThink(newText);

      // Nothing left after stripping (or an empty stream) → real failure, not a
      // blank bubble.
      if (!newText.trim()) {
        throw new Error('model returned no rewritten text');
      }

      // Update the element's raw text
      if (newText) {
        aiMsgElement.dataset.raw = newText;
        // Final render with proper markdown
        if (bodyEl) {
          bodyEl.innerHTML = markdownModule.processWithThinking(
            markdownModule.squashOutsideCode(newText)
          );
        }

        // Save the new response as a variant
        const nextVariants = [...variants, { raw: newText, html: bodyEl ? bodyEl.innerHTML : '', label: varLabel }];
        try {
          if (!rewriteId) throw new Error('Message identity unavailable');
          const metadataResponse = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sessionId)}/update-last-meta`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message_id: rewriteId, expected_hash: rewriteReceipt?.content_hash,
              expected_content: rewriteReceipt ? undefined : newText,
              metadata: { variants: nextVariants, variantIndex: nextVariants.length - 1 } }),
          });
          if (!metadataResponse.ok) throw new Error(`HTTP ${metadataResponse.status}`);
          if (sessionModule.getCurrentSessionId() !== sessionId || sessionModule.getSessionNavigationToken() !== rewriteNav || !aiMsgElement.isConnected || aiMsgElement.dataset.dbId !== rewriteId) return;
          variants = nextVariants;
          aiMsgElement.dataset.variants = JSON.stringify(variants);
          aiMsgElement.dataset.variantIndex = String(variants.length - 1);
          _renderVariantNav(aiMsgElement, variants, variants.length - 1);
        } catch (error) {
          if (sessionModule.getCurrentSessionId() === sessionId && sessionModule.getSessionNavigationToken() === rewriteNav && aiMsgElement.isConnected && uiModule) {
            uiModule.showError('Rewrite saved, but variant history could not be saved: ' + error.message);
          }
        }
      }

      if (uiModule) uiModule.scrollHistory();

    } catch (err) {
      console.error('Rewrite failed:', err);
      _killRwSpin();
      // Restore original content on failure
      if (bodyEl) bodyEl.innerHTML = oldHtml;
      if (uiModule) uiModule.showError('Rewrite failed: ' + err.message);
    }
  }

  /**
   * Continue the AI's response from where it left off.
   */
  export async function continueFrom(aiMsgElement) {
    const sessionId = sessionModule.getCurrentSessionId();
    if (!sessionId) return;

    const messageInput = uiModule.el('message');
    if (messageInput) {
      messageInput.value = 'Continue from where you left off.';
      const submitBtn = document.querySelector('.send-btn');
      if (submitBtn) submitBtn.click();
    }
  }

  // Open a chat attachment in the right place: images → Imps; PDFs &
  // text/code/markdown → Documents viewer; anything else → raw file. A given
  // upload's imported document is reused (cached by upload id) so clicking it
  // again re-opens the same doc instead of making duplicates.
  const _attachDocCache = new Map();  // upload id -> doc id
  function _attachLang(name) {
    const m = (name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
    const ext = m ? m[1] : '';
    const map = { md:'markdown', markdown:'markdown', js:'javascript', ts:'typescript',
      jsx:'javascript', tsx:'typescript', py:'python', rb:'ruby', go:'go', rs:'rust',
      java:'java', c:'c', cpp:'cpp', h:'c', hpp:'cpp', cs:'csharp', php:'php', html:'html',
      htm:'html', css:'css', scss:'scss', json:'json', yaml:'yaml', yml:'yaml', sh:'bash',
      bash:'bash', sql:'sql', csv:'csv', xml:'xml' };
    return map[ext] || '';
  }
  async function openAttachment(att, isImage) {
    if (!att || !att.id) return;
    const id = att.id, name = att.name || '', mime = att.mime || '';
    const url = `${API_BASE}/api/upload/${id}`;

    // Images → Imps.
    if (isImage) {
      return openAuthorizedImageSource('upload',String(id),name,()=>true,att.session_id || null);
    }

    const isPdf = mime === 'application/pdf' || /\.pdf$/i.test(name);
    const TEXT_EXT = /\.(txt|md|markdown|js|ts|jsx|tsx|py|rb|go|rs|java|c|cpp|h|hpp|cs|php|html?|css|scss|sass|less|json|ya?ml|toml|ini|conf|env|sh|bash|sql|csv|tsv|xml|log|vue|svelte)$/i;
    const isTextDoc = TEXT_EXT.test(name) || /^text\//.test(mime);
    if (!isPdf && !isTextDoc) { window.open(url, '_blank'); return; }  // binary/unknown → raw

    // Reuse the doc we already imported for this upload, if it still loads.
    const cached = _attachDocCache.get(id);
    if (cached) {
      try {
        documentModule.openPanel && documentModule.openPanel();
        await documentModule.loadDocument(cached);
        return;
      } catch (_) { _attachDocCache.delete(id); }
    }

    // Need a session to attach the doc to (bare-session fallback, same as compose).
    let sid = '';
    try { sid = sessionModule.getCurrentSessionId() || ''; } catch (_) {}
    if (!sid) {
      try {
        const _fd = new FormData();
        _fd.append('name', name || 'Attachment');
        _fd.append('skip_validation', 'true');
        const r = await fetch(`${API_BASE}/api/session`, { method: 'POST', body: _fd, credentials: 'same-origin' });
        if (r.ok) { const d = await r.json(); if (d && d.id) { sid = d.id; if (sessionModule.loadSessions) await sessionModule.loadSessions(); } }
      } catch (_) {}
    }

    try {
      let doc;
      if (isPdf) {
        // import-pdf wants a fresh file upload — re-fetch the stored blob and post it.
        const blob = await (await fetch(url)).blob();
        const fd = new FormData();
        fd.append('file', blob, name || 'document.pdf');
        if (sid) fd.append('session_id', sid);
        const res = await fetch(`${API_BASE}/api/documents/import-pdf`, { method: 'POST', body: fd, credentials: 'same-origin' });
        if (!res.ok) throw new Error('import-pdf ' + res.status);
        doc = await res.json();
      } else {
        const text = await (await fetch(url)).text();
        const res = await fetch(`${API_BASE}/api/document`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid || null, title: name.replace(/\.[^.]+$/, '') || 'Document', content: text, language: _attachLang(name) }),
        });
        if (!res.ok) throw new Error('document ' + res.status);
        doc = await res.json();
      }
      if (doc && doc.id) {
        _attachDocCache.set(id, doc.id);
        documentModule.openPanel && documentModule.openPanel();
        if (documentModule.injectFreshDoc) documentModule.injectFreshDoc(doc);
        else await documentModule.loadDocument(doc.id);
      }
    } catch (e) {
      console.error('open attachment as document failed', e);
      import('./ui.js').then(m => m.showError && m.showError('Could not open attachment')).catch(() => {});
      window.open(url, '_blank');  // fallback so the file is still reachable
    }
  }

  // Public API
  const chatModule = {
    init,
    initListeners,
    ratingAccountScope: () => { const value = captureCodeEvidenceDraft(); return {sid: value.sid, owner: value.owner, authEpoch: value.authEpoch}; },
    openAttachment,
    openAuthorizedImageSource,
    addMessage: chatRenderer.addMessage,
    displayMetrics: chatRenderer.displayMetrics,
    handleChatSubmit,
    sendBuddyExplanation: element => handleChatSubmit({preventDefault(){}},null,element),
    queueStreamingComposerRequest,
    submitOptionalQuestionAnswer,
    abortCurrentRequest,
    detachCurrentStream,
    checkBackgroundStream,
    resumeStream,
    restoreSessionState,
    hideWelcomeScreen: chatRenderer.hideWelcomeScreen,
    showWelcomeScreen: chatRenderer.showWelcomeScreen,
    checkPendingResearch,
    getImageCost: chatRenderer.getImageCost,
    setDisplayOverride,
    setHideUserBubble,
    setPendingContinue,
    regenerateFrom,
    forkFrom,
    editUserMessage,
    editAIMessage,
    resendUserMessage,
    deleteMessage,
    rewriteWith,
    continueFrom,
    _appendViewReportLink,
    hasActiveStream,
    captureComposerDraftState,
    captureCodeEvidenceDraft,
    appendCodeEvidence,
    attachGeneratedSummary,
    restoreComposerDraft,
  };

  // Single delegated handler for tool-call fold/expand. One listener on
  // document.body covers every .agent-thread-node — running, completed,
  // streaming, history-rendered, compare-mode, all of them. Re-attaching
  // per-node listeners on every innerHTML rewrite was the source of the
  // "needs many clicks" bug.
  if (!window.__odysseus_thread_click_bound) {
    document.body.addEventListener('click', (e) => {
      const header = e.target.closest('.agent-thread-header');
      if (!header) return;
      const node = header.closest('.agent-thread-node');
      if (!node) return;
      const opened = node.classList.toggle('open');
      if (opened) {
        // Expanding the final tool trace can push a pending ask_user card below
        // the viewport.  Keep that immediately-adjacent prompt visible.
        const thread = node.closest('.agent-thread');
        const pendingCard = thread?.nextElementSibling;
        if (pendingCard?.classList.contains('ask-user-card')) {
          requestAnimationFrame(() => pendingCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
        }
      }
    });
    window.__odysseus_thread_click_bound = true;
  }

  export default chatModule;
  window.chatModule = chatModule;
