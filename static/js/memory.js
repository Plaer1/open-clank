import { recordPresentation } from './achievementProducer.js';
import { uiIcon } from './uiIcons.js';
// Memory Management Functions
// This module handles all memory-related operations

import uiModule from './ui.js';
import sessionModule from './sessions.js';
import spinnerModule from './spinner.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { memoryChips, isTrusted, DEFAULT_KIND_TRUST } from './util/memoryTrust.js';
import { forceLayout, hitTest, tagCounts, mergeExpansion } from './util/memoryGraph.js';
import { contextualErrorMessage, responseError } from './util/httpError.js';
import { memoryAssetUrl } from './util/memoryExport.js';
import { createMemoryExportDialog, downloadMemoryAsset } from './memoryExportDialog.js';
import { dismissAllPendingImportBatches } from './memoryImportDismiss.js';

var escapeHtml = uiModule.esc;

let memories = [];
let activeCategory = 'all';
let sortOrder = 'newest';
let selectMode = false;
let selectedIds = new Set();
let memoriesLoading = false;
let memoryLoadError = null;
let memoryProviderStatus = '';
let inspectedMemories = [];
let pendingMemoryImportFile = null;
let pendingMemoryImportFiles = [];
let memoryImportRetrying = false;
// Per-user trust prefs (memory_trust_auto + memory_trust_auto_kinds),
// loaded with the list so the trusted/reference chip reflects reality.
let trustPrefsState = {};
// Signal filters (kind / provenance / trust) — fm-provider only.
let signalFilters = { kind: 'all', provenance: 'all', trust: 'all' };

async function _loadTrustPrefs() {
  try {
    const response = await fetch('/api/prefs', { credentials: 'same-origin' });
    if (response.ok) trustPrefsState = await response.json() || {};
  } catch { /* chips fall back to defaults (fail closed) */ }
}

function _renderMemoryProviderStatus() {
  const el = document.getElementById('memory-provider-status');
  if (el) el.textContent = memoryProviderStatus ? `· ${memoryProviderStatus}` : '';
}


const MEMORY_CATEGORIES = ['fact', 'identity', 'preference', 'contact', 'project', 'goal', 'task', 'unknown'];

// Display names where the raw category value would read wrong in UI.
// 'unknown' is the open-question kind, not "kind unknown".
const CATEGORY_LABELS = { unknown: 'open question' };
const categoryLabel = (cat) => CATEGORY_LABELS[cat] || cat;

// Sort-option icons for the custom Memory sort picker (and Skills picker
// once it reuses the same markup). Each value maps to a 13px Feather-style
// SVG so the icon visually distinguishes Newest / Oldest / A-Z / Most used.
const _MEMORY_SORT_ICONS = {
  newest: uiIcon("clock", 13),
  oldest: uiIcon("refresh", 13),
  alpha:  uiIcon("sort-alpha", 13),
  uses:   uiIcon("skills", 13),
};

function _memorySortIcon(value) {
  return _MEMORY_SORT_ICONS[value] || _MEMORY_SORT_ICONS.newest;
}

function _renderMemorySortPickerCurrent() {
  const sel = document.getElementById('memory-sort');
  const btn = document.getElementById('memory-sort-btn');
  if (!sel || !btn) return;
  const value = sel.value || 'newest';
  const opt = sel.querySelector(`option[value="${CSS.escape(value)}"]`);
  const label = opt ? opt.textContent : value;
  const iconWrap = btn.querySelector('.memory-sort-icon-cur');
  const labelEl = btn.querySelector('.memory-sort-label');
  if (iconWrap) iconWrap.innerHTML = _memorySortIcon(value);
  if (labelEl) labelEl.textContent = label;
}

function _initMemorySortPicker() {
  const sel = document.getElementById('memory-sort');
  const picker = document.getElementById('memory-sort-picker');
  const btn = document.getElementById('memory-sort-btn');
  const menu = document.getElementById('memory-sort-menu');
  if (!sel || !picker || !btn || !menu || picker._wired) return;
  picker._wired = true;

  const items = Array.from(sel.children)
    .filter(o => o.tagName === 'OPTION')
    .map(o => ({ value: o.value, label: o.textContent }));

  menu.innerHTML = items.map(it => `
    <button type="button" role="option" class="memory-sort-item" data-value="${it.value}">
      <span class="memory-sort-item-icon">${_memorySortIcon(it.value)}</span>
      <span class="memory-sort-item-label">${it.label}</span>
    </button>
  `).join('');

  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  const open  = () => { menu.hidden = false; btn.setAttribute('aria-expanded', 'true'); };

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.hidden) open(); else close();
  });
  menu.addEventListener('click', (e) => {
    const item = e.target.closest('.memory-sort-item');
    if (!item) return;
    sel.value = item.dataset.value;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    _renderMemorySortPickerCurrent();
    close();
  });
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !picker.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) {
      e.stopPropagation();
      close();
    }
  }, { capture: true });

  _renderMemorySortPickerCurrent();
}

function _ensureNewMemoryCategorySelect() {
  const sel = document.getElementById('new-memory-category');
  if (!sel || sel.dataset.wired === '1') return;
  sel.dataset.wired = '1';
  MEMORY_CATEGORIES.forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat;
    opt.textContent = categoryLabel(cat);
    if (cat === 'fact') opt.selected = true;
    sel.appendChild(opt);
  });
}

function _readNewMemoryCategory() {
  _ensureNewMemoryCategorySelect();
  const sel = document.getElementById('new-memory-category');
  const cat = sel?.value || 'fact';
  return MEMORY_CATEGORIES.includes(cat) ? cat : 'fact';
}

let _memoryDragWired = false;
function _wireMemoryDrag() {
  if (_memoryDragWired) return;
  const modal = document.getElementById('memory-modal');
  const content = modal && modal.querySelector('.modal-content');
  const header = modal && modal.querySelector('.modal-header');
  if (!modal || !content || !header) return;
  _memoryDragWired = true;
  makeWindowDraggable(modal, {
    content,
    header,
    skipSelector: 'button, input, select, label',
    enableDock: true,
    enableLeftDock: true,
    onEnterFullscreen: () => {
      snapModalToZone(modal, {
        name: 'fullscreen',
        rect: {
          left: 0,
          top: 0,
          width: window.innerWidth || document.documentElement.clientWidth || 0,
          height: window.innerHeight || document.documentElement.clientHeight || 0,
        },
      });
    },
  });
}

function relativeTime(timestamp) {
  const now = Math.floor(Date.now() / 1000);
  const diff = now - timestamp;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  if (diff < 2592000) return `${Math.floor(diff / 604800)}w ago`;
  if (diff < 31536000) return `${Math.floor(diff / 2592000)}mo ago`;
  return `${Math.floor(diff / 31536000)}y ago`;
}

function buildCategoryChips() {
  const container = document.getElementById('memory-category-filters');
  if (!container) return;

  // Hide the chip row entirely when there are no memories — no point showing
  // an "all" chip with nothing to filter.
  if (!memories.length) { container.innerHTML = ''; return; }

  const cats = new Set(memories.map(m => m.category || 'fact'));
  const sorted = ['all', ...Array.from(cats).sort()];

  container.innerHTML = '';
  sorted.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = 'memory-cat-chip' + (cat === activeCategory ? ' active' : '');
    btn.dataset.cat = cat;
    btn.textContent = cat === 'all' ? 'all' : categoryLabel(cat);
    btn.addEventListener('click', () => {
      activeCategory = cat;
      container.querySelectorAll('.memory-cat-chip').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderMemoryList();
      updateMemoryCount();
    });
    container.appendChild(btn);
  });
}

async function syncToggles() {
  // The settings tab no longer hosts a separate "Memory in context" toggle —
  // the header toggle owns that pref directly now.
  await syncPrefToggle('memory-enabled-header-toggle', 'memory_enabled', 'Memory enabled', 'Memory disabled', false);
  // The Skills header toggle owns the `skills_enabled` pref (was never wired —
  // toggling it did nothing, so skills stayed on). Now it actually gates skill
  // injection (see chat_helpers.py: uprefs.skills_enabled).
  await syncPrefToggle('skills-enabled-header-toggle', 'skills_enabled', 'Skills enabled', 'Skills disabled', false);
  await syncMemoryMode();
  await syncPrefToggle('memory-trust-auto-toggle', 'memory_trust_auto', 'Auto-captured memories can be trusted (per kind below)', 'Auto-captured memories stay behind the firewall', false);
  await _wireTrustKindSwitches();
  await syncPrefToggle('auto-skills-toggle', 'auto_skills', 'Auto-extract skills enabled', 'Auto-extract skills disabled', false);
  await syncPrefToggle('auto-approve-skills-toggle', 'auto_approve_skills', 'Auto-approve skills enabled', 'Auto-approve skills disabled', false);
  await syncPrefSlider('skill-confidence-slider', 'skill_min_confidence', 'skill-confidence-label', 0.85);
  await syncPrefNumber('skill-max-input', 'skill_max_injected', 3);
  await syncMobileControlSide();

  // Reflect the header toggle into the sidebar dim + modal body opacity.
  const headerToggle = document.getElementById('memory-enabled-header-toggle');
  if (headerToggle) {
    const modalBody = document.querySelector('.memory-modal-body');
    if (modalBody) modalBody.style.opacity = headerToggle.checked ? '' : '0.3';
    reflectMemoryToggleInSidebar(headerToggle.checked);
    if (!headerToggle.dataset.boundUx) {
      headerToggle.dataset.boundUx = '1';
      headerToggle.addEventListener('change', () => {
        if (modalBody) modalBody.style.opacity = headerToggle.checked ? '' : '0.3';
        reflectMemoryToggleInSidebar(headerToggle.checked);
      });
    }
  }

  // Same dim treatment for the Skills toggle — dims the skills panel when off.
  const skillsToggle = document.getElementById('skills-enabled-header-toggle');
  if (skillsToggle) {
    const skillsPanel = document.querySelector('[data-memory-panel="skills"]');
    const applyDim = () => { if (skillsPanel) skillsPanel.style.opacity = skillsToggle.checked ? '' : '0.3'; };
    applyDim();
    if (!skillsToggle.dataset.boundUx) {
      skillsToggle.dataset.boundUx = '1';
      skillsToggle.addEventListener('change', applyDim);
    }
  }
}

const _MOBILE_CONTROL_SIDES = new Set(['left', 'right', 'system']);

async function syncMobileControlSide() {
  const select = document.getElementById('memory-mobile-control-side');
  if (!select) return;
  let saved = 'system';
  try {
    const response = await fetch('/api/prefs/mobile_control_side', { credentials: 'same-origin' });
    if (response.ok) {
      const payload = await response.json();
      if (_MOBILE_CONTROL_SIDES.has(payload?.value)) saved = payload.value;
    }
  } catch (error) {
    console.debug('Unable to load mobile-control-side preference:', error);
  }
  select.value = saved;
  select.dataset.saved = saved;
  if (select.dataset.bound === '1') return;
  select.dataset.bound = '1';
  select.addEventListener('change', async () => {
    const next = _MOBILE_CONTROL_SIDES.has(select.value) ? select.value : 'system';
    const previous = _MOBILE_CONTROL_SIDES.has(select.dataset.saved)
      ? select.dataset.saved
      : 'system';
    select.disabled = true;
    try {
      const response = await fetch('/api/prefs/mobile_control_side', {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: next }),
      });
      if (!response.ok) throw new Error((await responseError(response, 'Failed to save mobile control preference')).message);
      select.value = next;
      select.dataset.saved = next;
      showToast(next === 'system'
        ? 'Mobile controls follow the device setting'
        : `Mobile controls: ${next}`);
    } catch (error) {
      console.error('Failed to save mobile-control-side preference:', error);
      select.value = previous;
      showError(error?.message || 'Failed to save mobile control preference');
    } finally {
      select.disabled = false;
    }
  });
}

function _setProfileQuestionStatus(message, tone = '') {
  const status = document.getElementById('memory-profile-question-status');
  if (!status) return;
  status.textContent = message || '';
  status.style.color = tone === 'error'
    ? 'var(--color-error, var(--red))'
    : (tone === 'success' ? 'var(--color-success, var(--green))' : '');
}

async function _addHandlerProfileQuestions() {
  const button = document.getElementById('memory-add-profile-questions');
  if (button?.dataset.busy === '1') return;
  if (button) {
    button.dataset.busy = '1';
    button.disabled = true;
  }
  _setProfileQuestionStatus('Preparing Handler questions…');
  try {
    const response = await fetch('/api/memory/principals', { credentials: 'same-origin' });
    if (!response.ok) throw new Error((await responseError(response, 'Handler identity is unavailable')).message);
    const payload = await response.json();
    const handler = (Array.isArray(payload?.principals) ? payload.principals : [])
      .find((principal) => principal?.role === 'Handler');
    if (!handler?.id) throw new Error('Handler identity is unavailable; try again after Memory reconnects.');
    const target = { kind: 'entity', id: String(handler.id) };
    const questions = [
      {
        text: "What name should I use for my Handler?",
        category: 'unknown',
        question_context: {
          mode: 'missing_slot',
          target,
          predicate: 'preferred_name',
          predicate_version: '1',
          claim_slot: 'identity.preferred_name',
          expected_value_type: 'string',
          question_id: 'handler-preferred-name',
        },
      },
      {
        text: 'Which side should my Handler prefer for mobile controls: left, right, or system default?',
        category: 'unknown',
        question_context: {
          mode: 'missing_slot',
          target,
          predicate: 'mobile_control_side',
          predicate_version: '1',
          claim_slot: 'ui.mobile_control_side',
          expected_value_type: 'string',
          question_id: 'handler-mobile-control-side',
        },
      },
    ];
    let added = 0;
    let already = 0;
    const failures = [];
    for (const question of questions) {
      try {
        const result = await fetch('/api/memory/add', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(question),
        });
        if (!result.ok) throw new Error((await responseError(result, 'Could not add profile question')).message);
        const saved = await result.json();
        if (saved?.memory_id || saved?.candidate_id) added += 1;
        else already += 1;
      } catch (error) {
        failures.push(error?.message || 'Could not add a profile question');
      }
    }
    if (failures.length) {
      const message = `${added + already} profile question${added + already === 1 ? '' : 's'} ready; ${failures.length} need attention.`;
      _setProfileQuestionStatus(message, 'error');
      showError(`${message} ${failures[0]}`);
      return;
    }
    const message = added
      ? `${added} profile question${added === 1 ? '' : 's'} added${already ? `; ${already} already existed` : ''}.`
      : 'Profile questions already exist.';
    _setProfileQuestionStatus(message, 'success');
    await loadMemories();
    showToast(message);
  } catch (error) {
    const message = error?.message || 'Could not prepare Handler profile questions';
    _setProfileQuestionStatus(message, 'error');
    showError(message);
  } finally {
    if (button) {
      button.dataset.busy = '0';
      button.disabled = false;
    }
  }
}

function _wireHandlerProfileControls() {
  document.getElementById('memory-add-profile-questions')?.addEventListener('click', _addHandlerProfileQuestions);
}

function reflectMemoryToggleInSidebar(enabled) {
  const btn = document.getElementById('tool-memory-btn');
  if (btn) btn.classList.toggle('tool-disabled', !enabled);
}

const MEMORY_NUKE_COMPONENTS = Object.freeze({
  memories: 'Memories',
  graph: 'Graph view data',
  ingest: 'Ingest pipeline state',
  skills: 'Skills',
});
let memoryNukeBusy = false;

function _memoryNukeCheckboxes() {
  return Array.from(document.querySelectorAll('[data-memory-nuke-component]'));
}

function _selectedMemoryNukeComponents() {
  return _memoryNukeCheckboxes()
    .filter(input => input.checked && Object.hasOwn(MEMORY_NUKE_COMPONENTS, input.dataset.memoryNukeComponent))
    .map(input => input.dataset.memoryNukeComponent);
}

function _setMemoryNukeStatus(message, tone = '') {
  const status = document.getElementById('memory-nuke-status');
  if (!status) return;
  status.textContent = message || '';
  status.style.color = tone === 'error'
    ? 'var(--color-error)'
    : (tone === 'success' ? 'var(--color-success, #62c48d)' : '');
}

function _updateMemoryNukeControls() {
  const selected = _selectedMemoryNukeComponents();
  const nukeButton = document.getElementById('memory-nuke-btn');
  if (nukeButton) {
    nukeButton.disabled = memoryNukeBusy || selected.length === 0;
    nukeButton.textContent = memoryNukeBusy
      ? 'Working…'
      : (selected.length === Object.keys(MEMORY_NUKE_COMPONENTS).length ? 'Nuke all my Brain data' : 'Nuke selected');
    nukeButton.setAttribute('aria-busy', String(memoryNukeBusy));
  }
  for (const input of _memoryNukeCheckboxes()) input.disabled = memoryNukeBusy;
  for (const id of ['memory-nuke-all', 'memory-nuke-none']) {
    const button = document.getElementById(id);
    if (button) button.disabled = memoryNukeBusy;
  }
}

function _setMemoryNukeSelection(checked) {
  for (const input of _memoryNukeCheckboxes()) input.checked = checked;
  _updateMemoryNukeControls();
}

function _boundedMemoryNukeText(value, maxLength = 280) {
  const compact = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  if (!compact) return '';
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact;
}

function _memoryNukeCount(detail) {
  const value = typeof detail === 'number' ? detail : detail?.count;
  return Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : null;
}

function _memoryNukeExpandedComponents(value) {
  if (Array.isArray(value)) return value.filter(key => typeof key === 'string');
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value)
    .filter(([, included]) => included !== false && included !== null)
    .map(([key]) => key);
}

function _memoryNukeImplications(value) {
  const items = Array.isArray(value) ? value : (value && typeof value === 'object' ? Object.values(value) : []);
  return items.map(item => {
    if (typeof item === 'string') return _boundedMemoryNukeText(item);
    return _boundedMemoryNukeText(item?.message || item?.detail || item?.description);
  }).filter(Boolean);
}

function _memoryNukeLabel(component) {
  return MEMORY_NUKE_COMPONENTS[component]
    || String(component || '').replaceAll('_', ' ').replace(/^./, char => char.toUpperCase());
}

function _memoryNukeConfirmationMessage(preview, requested) {
  const expanded = _memoryNukeExpandedComponents(preview.expanded_components);
  const components = preview.components && typeof preview.components === 'object'
    ? preview.components
    : {};
  const included = [...new Set((expanded.length ? expanded : requested).filter(Boolean))];
  const lines = ['This permanently deletes the following data owned by your account:'];
  for (const component of included) {
    const count = _memoryNukeCount(components[component]);
    lines.push(`• ${_memoryNukeLabel(component)}${count === null ? '' : ` — ${count} ${count === 1 ? 'record' : 'records'}`}`);
  }
  const added = included.filter(component => !requested.includes(component));
  if (added.length) {
    lines.push('', `Required additions: ${added.map(_memoryNukeLabel).join(', ')}`);
  }
  const implications = _memoryNukeImplications(preview.implications);
  if (implications.length) {
    lines.push('', 'What this means:');
    for (const implication of implications.slice(0, 6)) lines.push(`• ${implication}`);
    if (implications.length > 6) lines.push(`• ${implications.length - 6} more implications`);
  }
  lines.push('', 'This cannot be undone in the live Brain. Existing exports, backups, and source files remain. Other users and built-in or shared skills are not touched.');
  return lines.join('\n');
}

async function _postMemoryNuke(payload, fallback) {
  const response = await fetch('/api/memory/nuke', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const failure = await responseError(response, fallback);
    const error = new Error(failure.message);
    error.problem = failure.problem;
    throw error;
  }
  try {
    return await response.json();
  } catch {
    throw new Error(`${fallback}; server returned an invalid response`);
  }
}

function _validateMemoryNukePreview(preview) {
  const valid = preview
    && preview.status === 'preview'
    && preview.complete === false
    && typeof preview.operation_id === 'string'
    && preview.operation_id.length > 0
    && typeof preview.preview_token === 'string'
    && preview.preview_token.length > 0
    && typeof preview.confirmation === 'string'
    && preview.confirmation.length > 0
    && preview.components
    && typeof preview.components === 'object';
  if (!valid) throw new Error('Could not preview Brain reset; server returned an incomplete response');
}

function _validateMemoryNukeCommit(result) {
  const valid = result
    && typeof result.complete === 'boolean'
    && ((result.complete === true && result.status === 'complete')
      || (result.complete === false && result.status === 'partial'))
    && result.categories
    && typeof result.categories === 'object';
  if (!valid) throw new Error('Could not verify Brain reset; server returned an incomplete response');
}

function _memoryNukeResultSummary(result) {
  const parts = [];
  for (const component of Object.keys(MEMORY_NUKE_COMPONENTS)) {
    const detail = result.categories?.[component];
    if (!detail) continue;
    const count = _memoryNukeCount(detail);
    const state = _boundedMemoryNukeText(detail.state, 60);
    const error = _boundedMemoryNukeText(detail.error, 180);
    parts.push(`${_memoryNukeLabel(component)}${count === null ? '' : ` ${count}`}${state ? ` (${state})` : ''}${error ? ` — ${error}` : ''}`);
  }
  const serverMessage = _boundedMemoryNukeText(result.message, 400);
  if (serverMessage && parts.length) return `${serverMessage} ${parts.join(' · ')}`;
  if (serverMessage) return serverMessage;
  if (parts.length) return parts.join(' · ');
  return result.complete ? 'Selected Brain data was deleted.' : 'Brain reset finished only partially.';
}

async function _refreshMemoryNukeSurfaces() {
  const refreshes = [
    Promise.resolve().then(() => loadMemories()),
    Promise.resolve().then(() => loadMemoryInspect()),
    Promise.resolve().then(() => loadMemoryGraph()),
    Promise.resolve().then(() => loadDigestPreview()),
    import('./skills.js').then(module => {
      const loadSkills = module.loadSkills || module.default?.loadSkills;
      return typeof loadSkills === 'function' ? loadSkills(false) : undefined;
    }),
  ];
  await Promise.allSettled(refreshes);
}

async function _nukeSelectedMemoryData() {
  if (memoryNukeBusy) return;
  const components = _selectedMemoryNukeComponents();
  if (!components.length) {
    _updateMemoryNukeControls();
    return;
  }

  memoryNukeBusy = true;
  _setMemoryNukeStatus('Counting the selected data…');
  _updateMemoryNukeControls();
  try {
    const preview = await _postMemoryNuke({ action: 'preview', components }, 'Could not preview Brain reset');
    _validateMemoryNukePreview(preview);
    const confirmed = await uiModule.styledConfirm(
      _memoryNukeConfirmationMessage(preview, components),
      {
        title: 'Nuke selected Brain data?',
        confirmText: 'Nuke selected data',
        cancelText: 'Keep my data',
        danger: true,
      },
    );
    if (!confirmed) {
      _setMemoryNukeStatus('Nothing was deleted.');
      return;
    }

    _setMemoryNukeStatus('Permanently deleting the selected data…');
    const result = await _postMemoryNuke({
      action: 'commit',
      operation_id: preview.operation_id,
      preview_token: preview.preview_token,
      confirmation: preview.confirmation,
    }, 'Could not complete Brain reset');
    _validateMemoryNukeCommit(result);

    // Even a partial commit may have changed every visible Brain surface.
    await _refreshMemoryNukeSurfaces();
    const summary = _memoryNukeResultSummary(result);
    if (result.complete === true) {
      _setMemoryNukeSelection(false);
      _setMemoryNukeStatus(summary, 'success');
      showToast('Selected Brain data deleted');
    } else {
      _setMemoryNukeStatus(summary, 'error');
      showError(contextualErrorMessage('Brain reset was only partially completed', summary));
    }
  } catch (error) {
    const message = contextualErrorMessage('Could not nuke Brain data', error?.message);
    _setMemoryNukeStatus(message, 'error');
    showError(message);
  } finally {
    memoryNukeBusy = false;
    _updateMemoryNukeControls();
  }
}

function _wireMemoryNukeControls() {
  for (const input of _memoryNukeCheckboxes()) {
    input.addEventListener('change', _updateMemoryNukeControls);
  }
  document.getElementById('memory-nuke-all')?.addEventListener('click', () => _setMemoryNukeSelection(true));
  document.getElementById('memory-nuke-none')?.addEventListener('click', () => _setMemoryNukeSelection(false));
  document.getElementById('memory-nuke-btn')?.addEventListener('click', _nukeSelectedMemoryData);
  _updateMemoryNukeControls();
}

// T7 trust panel: six per-kind switches under the master toggle. Kind
// switch state persists while the master is off (rows just dim). Human-
// authored and pinned memories are always trusted; these govern
// auto-capture only.
const _TRUST_KIND_COPY = {
  instruction: 'Instructions — standing orders that steer behavior',
  persona: 'Persona — facts about who the AI is',
  fact: 'Facts — knowledge about you and your world',
  episodic: 'Episodes — records of past events',
  fabric: 'Fabric — threads connecting chats over time',
  wiki: 'Wiki — long-form authored notes',
};

async function _wireTrustKindSwitches() {
  const host = document.getElementById('memory-trust-kinds');
  const master = document.getElementById('memory-trust-auto-toggle');
  if (!host || !master) return;

  let kinds = { ...DEFAULT_KIND_TRUST };
  try {
    const response = await fetch('/api/prefs/memory_trust_auto_kinds', { credentials: 'same-origin' });
    if (response.ok) {
      const data = await response.json();
      if (data && data.value && typeof data.value === 'object') {
        for (const key of Object.keys(kinds)) {
          if (key in data.value) kinds[key] = Boolean(data.value[key]);
        }
      }
    }
  } catch { /* defaults stand */ }

  const dim = () => { host.style.opacity = master.checked ? '' : '0.4'; };
  dim();
  if (!master.dataset.boundTrustDim) {
    master.dataset.boundTrustDim = '1';
    master.addEventListener('change', () => {
      dim();
      // Chips reflect the new trust state immediately.
      trustPrefsState.memory_trust_auto = master.checked;
      renderMemoryList();
    });
  }

  if (!host.dataset.built) {
    host.dataset.built = '1';
    for (const kind of Object.keys(_TRUST_KIND_COPY)) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;';
      const label = document.createElement('span');
      label.className = 'admin-toggle-sub';
      label.style.margin = '0';
      label.textContent = _TRUST_KIND_COPY[kind];
      const wrap = document.createElement('label');
      wrap.className = 'admin-switch';
      wrap.style.flexShrink = '0';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.id = `memory-trust-kind-${kind}`;
      box.checked = kinds[kind];
      const slider = document.createElement('span');
      slider.className = 'admin-slider';
      wrap.appendChild(box);
      wrap.appendChild(slider);
      row.appendChild(label);
      row.appendChild(wrap);
      host.appendChild(row);
      box.addEventListener('change', async () => {
        kinds[kind] = box.checked;
        try {
          const response = await fetch('/api/prefs/memory_trust_auto_kinds', {
            method: 'PUT',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: kinds }),
          });
          if (!response.ok) throw new Error('save failed');
          trustPrefsState.memory_trust_auto_kinds = { ...kinds };
          renderMemoryList();
        } catch {
          box.checked = !box.checked;
          kinds[kind] = box.checked;
          showError('Could not save trust setting');
        }
      });
    }
  } else {
    for (const kind of Object.keys(_TRUST_KIND_COPY)) {
      const box = document.getElementById(`memory-trust-kind-${kind}`);
      if (box) box.checked = kinds[kind];
    }
  }
}

function syncToggleDim(toggle) {
  const card = toggle.closest('.admin-card');
  if (!card) return;
  const toggleRow = toggle.closest('div[style*="justify-content"]');
  let sibling = toggleRow ? toggleRow.nextElementSibling : null;
  while (sibling) {
    sibling.style.opacity = toggle.checked ? '' : '0.35';
    sibling.style.pointerEvents = toggle.checked ? '' : 'none';
    sibling = sibling.nextElementSibling;
  }
}

/** Load/save a confidence slider backed by a float pref (0 = "All", else
 *  0.50–1.00). Slider position is the percent; the MAX position means "All"
 *  (no minimum), and sliding down sets the bar to 95%, 90%, 85%… */
async function syncPrefSlider(elementId, prefKey, labelId, defaultVal) {
  const slider = document.getElementById(elementId);
  if (!slider) return;
  const label = labelId ? document.getElementById(labelId) : null;
  const maxPos = Number(slider.max);
  const fmt = (pos) => (Number(pos) >= maxPos ? 'All' : `≥ ${pos}%`);
  try {
    const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`);
    if (res.ok) {
      const data = await res.json();
      let pref = (data.value === undefined || data.value === null) ? defaultVal : Number(data.value);
      // pref 0 (or falsy) = "All" → max slider position; else percent.
      let pos = (!pref || pref <= 0) ? maxPos : Math.round(pref * 100);
      pos = Math.max(Number(slider.min), Math.min(maxPos, pos));
      slider.value = String(pos);
    }
  } catch (e) {
    console.error(`Failed to load ${prefKey} pref:`, e);
  }
  if (label) label.textContent = fmt(slider.value);
  if (!slider.dataset.bound) {
    slider.dataset.bound = '1';
    slider.addEventListener('input', () => { if (label) label.textContent = fmt(slider.value); });
    slider.addEventListener('change', async () => {
      const pos = Number(slider.value);
      const pref = pos >= maxPos ? 0 : pos / 100;
      try {
        const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: pref })
        });
        if (!res.ok) { showError('Failed to save preference'); return; }
        showToast(pref === 0 ? 'Skill confidence: All' : `Skill confidence ≥ ${Math.round(pref * 100)}%`);
      } catch (e) {
        console.error(`Failed to save ${prefKey} pref:`, e);
        showError('Failed to save preference');
      }
    });
  }
}

/** Load/save an integer-valued pref backed by a <input type="number">. */
async function syncPrefNumber(elementId, prefKey, defaultVal) {
  const input = document.getElementById(elementId);
  if (!input) return;
  const clamp = (raw) => {
    let v = parseInt(raw, 10);
    if (isNaN(v)) v = defaultVal;
    const lo = Number(input.min), hi = Number(input.max);
    if (!isNaN(lo)) v = Math.max(lo, v);
    if (!isNaN(hi)) v = Math.min(hi, v);
    return v;
  };
  try {
    const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`);
    if (res.ok) {
      const data = await res.json();
      input.value = String((data.value === undefined || data.value === null) ? defaultVal : clamp(data.value));
    }
  } catch (e) {
    console.error(`Failed to load ${prefKey} pref:`, e);
  }
  if (!input.dataset.bound) {
    input.dataset.bound = '1';
    input.addEventListener('change', async () => {
      const v = clamp(input.value);
      input.value = String(v);
      try {
        const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: v })
        });
        if (!res.ok) { showError('Failed to save preference'); return; }
        showToast(v === 0 ? 'No skills injected' : `Max injected skills: ${v}`);
      } catch (e) {
        console.error(`Failed to save ${prefKey} pref:`, e);
        showError('Failed to save preference');
      }
    });
  }
}

async function syncMemoryMode() {
  const select = document.getElementById('memory-mode-select');
  const help = document.getElementById('memory-mode-help');
  if (!select) return;
  const copy = {
    automatic: 'Capture useful memories automatically. Agent saves are admitted immediately.',
    manual: 'Capture and Agent saves wait in Candidates until you approve them.',
    off: 'Do not capture, inject, recall, or let the Agent change memories.',
  };
  try {
    const res = await fetch(`${window.location.origin}/api/prefs/memory_mode`);
    if (res.ok) {
      const data = await res.json();
      if (Object.hasOwn(copy, data.value)) {
        select.value = data.value;
      } else {
        const legacy = await fetch(`${window.location.origin}/api/prefs/auto_memory`);
        const legacyData = legacy.ok ? await legacy.json() : {};
        select.value = legacyData.value === false ? 'off' : 'automatic';
      }
    }
  } catch (e) {
    console.error('Failed to load memory mode:', e);
  }
  if (help) help.textContent = copy[select.value];
  if (!select.dataset.bound) {
    select.dataset.bound = '1';
    select.addEventListener('change', async () => {
      const previous = Object.hasOwn(copy, select.dataset.saved)
        ? select.dataset.saved
        : 'automatic';
      try {
        const res = await fetch(`${window.location.origin}/api/prefs/memory_mode`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: select.value }),
        });
        if (!res.ok) throw new Error(`PUT memory_mode returned ${res.status}`);
        select.dataset.saved = select.value;
        if (help) help.textContent = copy[select.value];
        showToast(`Memory mode: ${select.options[select.selectedIndex].text}`);
      } catch (e) {
        console.error('Failed to save memory mode:', e);
        select.value = previous;
        if (help) help.textContent = copy[select.value];
        showError('Failed to save preference');
      }
    });
  }
  select.dataset.saved = select.value;
}

async function syncPrefToggle(elementId, prefKey, onMsg, offMsg, dimBelow = true) {
  const toggle = document.getElementById(elementId);
  if (!toggle) return;
  try {
    const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`);
    if (res.ok) {
      const data = await res.json();
      toggle.checked = data.value !== false;
    }
  } catch (e) {
    console.error(`Failed to load ${prefKey} pref:`, e);
  }
  if (dimBelow) syncToggleDim(toggle);
  if (!toggle.dataset.bound) {
    toggle.dataset.bound = '1';
    toggle.addEventListener('change', async () => {
      if (dimBelow) syncToggleDim(toggle);
      try {
        const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: toggle.checked })
        });
        if (!res.ok) {
          console.error(`PUT ${prefKey} returned ${res.status}`);
          toggle.checked = !toggle.checked; // revert
          if (dimBelow) syncToggleDim(toggle);
          showError('Failed to save preference');
          return;
        }
        showToast(toggle.checked ? onMsg : offMsg);
      } catch (e) {
        console.error(`Failed to save ${prefKey} pref:`, e);
        toggle.checked = !toggle.checked; // revert
        if (dimBelow) syncToggleDim(toggle);
        showError('Failed to save preference');
      }
    });
  }
}

async function fetchMemoryPages() {
  const memory = [];
  let cursor = null;
  let provider = 'native';
  do {
    const params = new URLSearchParams({ limit: '1000' });
    if (cursor) params.set('cursor', cursor);
    const response = await fetch(`${window.location.origin}/api/memory?${params}`);
    if (!response.ok) {
      const error = new Error(`Memory provider unavailable (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    const page = Array.isArray(data) ? data : (data.memory || []);
    memory.push(...page);
    provider = data?.provider || provider;
    cursor = data?.next_cursor || null;
  } while (cursor);
  return { memory, provider };
}

export async function loadMemories() {
  _ensureNewMemoryCategorySelect();
  memoriesLoading = true;
  renderMemoryList();
  updateMemoryCount();
  try {
    const [data] = await Promise.all([fetchMemoryPages(), _loadTrustPrefs()]);
    memoryLoadError = null;
    memoryProviderStatus = data?.provider || 'native';
    _renderMemoryProviderStatus();

    if (data && data.memory) {
      memories = data.memory;
    } else if (Array.isArray(data)) {
      memories = data;
    } else {
      memories = [];
    }

    memoriesLoading = false;
    buildCategoryChips();
    _syncSignalFilterVisibility();
    renderMemoryList();
    updateMemoryCount();
  } catch (error) {
    console.error('Failed to load memories:', error);
    memoryLoadError = 'Memory provider unavailable';
    memoryProviderStatus = 'unavailable';
    _renderMemoryProviderStatus();
    memories = [];
    memoriesLoading = false;
    buildCategoryChips();
    renderMemoryList();
    updateMemoryCount();
  }
  // Always wire toggles, even if memory API failed
  syncToggles();
  // Surface durable import batches whose review was stranded (for example by
  // a proxy read timeout on the long-running import POST).
  _refreshPendingImportNotice();
}

function _inspectText(item, tier) {
  if (tier === 'candidate') return item.content || '';
  if (tier === 'quarantine') return item.content || '';
  if (tier === 'history') return item.current?.text || item.latest?.text || '';
  return item.content || item.text || '';
}

function _inspectMeta(item, tier) {
  const meta = item.metadata || item.payload || {};
  const provenance = meta.provenance || {};
  const sessionId = item.session_id || provenance.session_id;
  const parts = [
    tier === 'raw' ? meta.role : (item.current?.status || item.status),
    item.current?.kind || item.kind || item.tier,
    tier === 'history' ? `revision ${item.current_revision || item.latest_revision || '?'}` : null,
    item.source_type,
    item.owner || 'ownerless legacy',
    item.workspace_id || 'global',
    item.source || provenance.source,
    sessionId ? `from ${sessionId}` : null,
    item.reason || meta.admission_reason,
  ].filter(Boolean);
  return parts.join(' · ');
}

function _memoryError(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail;
  if (typeof data?.detail?.message === 'string') return data.detail.message;
  return fallback;
}

async function _runVersionedAction(item, action, fields = {}) {
  const id = item.id || item.block_id;
  const body = new URLSearchParams({
    expected_revision: String(item.current_revision || item.latest_revision),
    ...Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, String(value)])),
  });
  const response = await fetch(`/api/memory/${encodeURIComponent(id)}/${action}`, {
    method: 'POST',
    credentials: 'same-origin',
    body,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(_memoryError(data, `Memory ${action} failed`));
  await Promise.all([loadMemoryInspect(), loadMemories()]);
  return data;
}

function _editInspectedCandidate(card, item) {
  const editor = document.createElement('div');
  editor.className = 'memory-inline-editor';
  const text = document.createElement('textarea');
  text.className = 'memory-item-edit-input';
  text.rows = 3;
  // The candidate list renders identity tokens for display; the editor must
  // round-trip the stored (raw) form.
  text.value = item.raw_content || item.content || '';
  text.setAttribute('aria-label', 'Candidate text');
  const category = document.createElement('select');
  category.className = 'memory-edit-cat-select';
  category.setAttribute('aria-label', 'Candidate category');
  for (const name of MEMORY_CATEGORIES) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = categoryLabel(name);
    option.selected = name === (item.kind || item.category);
    category.append(option);
  }
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'memory-toolbar-btn';
  save.textContent = 'Save candidate';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'memory-toolbar-btn';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', renderMemoryInspect);
  save.addEventListener('click', async () => {
    const content = text.value.trim();
    if (!content) return showError('Candidate text cannot be empty');
    save.disabled = true;
    try {
      const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}`, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: content, category: category.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(_memoryError(data, 'Candidate edit failed'));
      showToast('Candidate updated');
      await loadMemoryInspect();
    } catch (error) {
      showError(error.message || 'Candidate edit failed');
      save.disabled = false;
    }
  });
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:6px;margin-top:8px';
  actions.append(save, cancel);
  editor.append(text, category, actions);
  card.replaceChildren(editor);
}

async function reviewInspectedCandidate(item, accept, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        accept,
        reason: accept ? 'approved_by_user' : 'rejected_by_user',
        owner: item.owner,
        workspace_id: item.workspace_id,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Review failed');
    showToast(accept ? 'Candidate promoted to Curated' : 'Candidate rejected');
    await Promise.all([loadMemoryInspect(), loadMemories()]);
  } catch (error) {
    showError(error.message || 'Review failed');
    button.disabled = false;
  }
}

function renderMemoryInspect() {
  const list = document.getElementById('memory-inspect-list');
  const tier = document.getElementById('memory-inspect-tier')?.value || 'raw';
  if (!list) return;
  list.replaceChildren();
  if (!inspectedMemories.length) {
    const empty = document.createElement('div');
    empty.className = 'memory-empty';
    empty.textContent = `No ${tier === 'raw' ? 'raw trajectory' : tier} records.`;
    list.append(empty);
    return;
  }
  inspectedMemories.forEach(item => {
    const card = document.createElement('div');
    card.className = 'memory-item';
    const text = document.createElement('div');
    text.className = 'memory-item-text';
    text.textContent = _inspectText(item, tier);
    const meta = document.createElement('div');
    meta.className = 'admin-toggle-sub';
    meta.style.marginTop = '6px';
    meta.textContent = _inspectMeta(item, tier);
    card.append(text, meta);
    if (tier === 'curated') {
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;margin-top:8px';
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'memory-toolbar-btn';
      edit.textContent = 'Edit';
      edit.addEventListener('click', () => {
        const record = {
          ...item,
          text: _inspectText(item, tier),
          // The editor round-trips the stored form, not the rendered display.
          raw_text: item.raw_text || item.raw_content || item.raw_headline
            || _inspectText(item, tier),
          category: item.category || _categoryForRecord(item),
        };
        _startMemoryCardEditor(card, record, {
          cancel: renderMemoryInspect,
          refreshInspect: true,
        });
      });
      actions.append(edit);
      card.append(actions);
    }
    if (tier === 'candidate' && item.status === 'pending') {
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;margin-top:8px';
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'memory-toolbar-btn';
      edit.textContent = 'Edit';
      edit.addEventListener('click', () => _editInspectedCandidate(card, item));
      const accept = document.createElement('button');
      accept.type = 'button';
      accept.className = 'memory-toolbar-btn';
      accept.textContent = 'Promote';
      accept.addEventListener('click', () => reviewInspectedCandidate(item, true, accept));
      const reject = document.createElement('button');
      reject.type = 'button';
      reject.className = 'memory-toolbar-btn danger';
      reject.textContent = 'Reject';
      reject.addEventListener('click', () => reviewInspectedCandidate(item, false, reject));
      actions.append(edit, accept, reject);
      card.append(actions);
    }
    if (tier === 'history') {
      const revisions = document.createElement('div');
      revisions.className = 'memory-details-drawer';
      for (const revision of (item.history || []).slice(0, 5)) {
        revisions.appendChild(_detailRow(
          `Revision ${revision.revision} · ${revision.status}`,
          revision.text || '(no value)',
        ));
      }
      card.append(revisions);
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;margin-top:8px;flex-wrap:wrap';
      const current = item.current || item.latest || {};
      if (current.kind === 'open_question' && current.status === 'active') {
        const reopen = document.createElement('button');
        reopen.type = 'button';
        reopen.className = 'memory-toolbar-btn';
        reopen.textContent = 'Reopen question';
        reopen.addEventListener('click', async () => {
          try {
            await _runVersionedAction(item, 'reopen');
            showToast('Question reopened');
          } catch (error) { showError(error.message); }
        });
        actions.append(reopen);
      }
      if (['active', 'open'].includes(current.status)) {
        const retract = document.createElement('button');
        retract.type = 'button';
        retract.className = 'memory-toolbar-btn danger';
        retract.textContent = 'Retract';
        retract.addEventListener('click', async () => {
          try {
            await _runVersionedAction(item, 'retract', { reason: 'retracted by user' });
            showToast('Memory retracted');
          } catch (error) { showError(error.message); }
        });
        actions.append(retract);
      }
      const prior = (item.history || []).find(revision => revision.revision !== item.current_revision);
      if (prior) {
        const revert = document.createElement('button');
        revert.type = 'button';
        revert.className = 'memory-toolbar-btn';
        revert.textContent = `Restore revision ${prior.revision}`;
        revert.addEventListener('click', async () => {
          try {
            await _runVersionedAction(item, 'revert', { target_revision: prior.revision });
            showToast(`Restored revision ${prior.revision}`);
          } catch (error) { showError(error.message); }
        });
        actions.append(revert);
      }
      if (actions.childNodes.length) card.append(actions);
    }
    list.append(card);
  });
}

export async function loadMemoryInspect() {
  const tier = document.getElementById('memory-inspect-tier')?.value || 'raw';
  const statusSelect = document.getElementById('memory-inspect-status');
  if (statusSelect) {
    const mode = statusSelect.dataset.mode;
    if (mode !== tier && ['candidate', 'history'].includes(tier)) {
      const values = tier === 'candidate'
        ? [['', 'All candidate states'], ['pending', 'Pending'], ['accepted', 'Accepted'], ['rejected', 'Rejected'], ['quarantined', 'Quarantined']]
        : [['', 'All knowledge states'], ['open', 'Open'], ['active', 'Active'], ['superseded', 'Superseded'], ['retracted', 'Retracted']];
      statusSelect.replaceChildren(...values.map(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        return option;
      }));
      statusSelect.dataset.mode = tier;
    }
    statusSelect.hidden = !['candidate', 'history'].includes(tier);
  }
  const status = ['candidate', 'history'].includes(tier) ? (statusSelect?.value || '') : '';
  const list = document.getElementById('memory-inspect-list');
  if (list) list.textContent = 'Loading…';
  try {
    const [tierResponse, qualityResponse] = await Promise.all([
      fetch(`/api/memory/inspect?tier=${encodeURIComponent(tier)}${status ? `&status=${encodeURIComponent(status)}` : ''}`, { credentials: 'same-origin' }),
      fetch('/api/memory/quality', { credentials: 'same-origin' }),
    ]);
    if (!tierResponse.ok || !qualityResponse.ok) throw new Error('Memory inspection unavailable');
    const tierData = await tierResponse.json();
    const quality = await qualityResponse.json();
    inspectedMemories = Array.isArray(tierData.items) ? tierData.items : [];
    const graph = quality.graph || {};
    const qualityEl = document.getElementById('memory-quality');
    if (qualityEl) {
      qualityEl.textContent = `Raw ${quality.raw || 0} · Candidates ${quality.candidates || 0} · Curated ${quality.curated || 0} · Quarantined ${quality.quarantined || 0} · Index ${graph.integrity_ok ? 'healthy' : 'needs rebuild'} (${graph.cues || 0}/${graph.cue_fts || 0}, ${graph.orphan_cues || 0} orphans)`;
    }
    renderMemoryInspect();
  } catch (error) {
    inspectedMemories = [];
    if (list) list.textContent = error.message || 'Memory inspection unavailable';
  }
}

// ---- Bulk select mode ----

const _SELECT_BTN_DOT_SVG = uiIcon("select", 11, {"className":"memory-select-btn-icon","style":"vertical-align:-2px;margin-right:3px;"});
const _SELECT_BTN_X_SVG = uiIcon("close", 11, {"className":"memory-select-btn-icon","style":"vertical-align:-2px;margin-right:3px;"});

function enterSelectMode() {
  selectMode = true;
  selectedIds.clear();
  const bulkBar = document.getElementById('memory-bulk-bar');
  const selectBtn = document.getElementById('memory-select-btn');
  if (bulkBar) bulkBar.classList.remove('hidden');
  if (selectBtn) { selectBtn.classList.add('active'); selectBtn.innerHTML = _SELECT_BTN_X_SVG + 'Cancel'; }
  updateBulkCount();
  renderMemoryList();
}

function exitSelectMode() {
  selectMode = false;
  selectedIds.clear();
  const bulkBar = document.getElementById('memory-bulk-bar');
  const selectBtn = document.getElementById('memory-select-btn');
  const selectAll = document.getElementById('memory-select-all');
  if (bulkBar) bulkBar.classList.add('hidden');
  if (selectBtn) { selectBtn.classList.remove('active'); selectBtn.innerHTML = _SELECT_BTN_DOT_SVG + 'Select'; }
  if (selectAll) selectAll.checked = false;
  renderMemoryList();
}

function toggleSelectItem(id) {
  if (selectedIds.has(id)) {
    selectedIds.delete(id);
  } else {
    selectedIds.add(id);
  }
  updateBulkCount();
}

function updateBulkCount() {
  const countEl = document.getElementById('memory-selected-count');
  const deleteBtn = document.getElementById('memory-bulk-delete');
  if (countEl) countEl.textContent = `${selectedIds.size} Selected`;
  if (deleteBtn) deleteBtn.disabled = selectedIds.size === 0;
}

function toggleSelectAll() {
  const selectAllEl = document.getElementById('memory-select-all');
  if (!selectAllEl) return;

  if (selectAllEl.checked) {
    // Select all currently visible/filtered items
    const visible = getFilteredMemories();
    visible.forEach(m => selectedIds.add(m.id));
  } else {
    selectedIds.clear();
  }
  updateBulkCount();
  renderMemoryList();
}

async function bulkDelete() {
  if (selectedIds.size === 0) return;
  const count = selectedIds.size;
  if (!await uiModule.styledConfirm(`Delete ${count} ${count === 1 ? 'memory' : 'memories'}?`, { confirmText: 'Delete', danger: true })) return;

  let deleted = 0;
  const deletedIds = [];
  for (const id of selectedIds) {
    try {
      const res = await fetch(`${window.location.origin}/api/memory/${id}`, { method: 'DELETE' });
      if (res.ok) {
        deleted++;
        deletedIds.push(id);
      }
    } catch (e) {
      console.error('Failed to delete memory:', id, e);
    }
  }

  await animateMemoryRemoval(deletedIds);
  exitSelectMode();
  await loadMemories();
  showToast(`Deleted ${deleted} ${deleted === 1 ? 'memory' : 'memories'}`);
}

// ---- Tidy (audit) ----

export async function tidyMemories() {
  const tidyBtn = document.getElementById('memory-tidy-btn');
  let tidySpinner = null;
  if (tidyBtn) {
    tidyBtn.disabled = true;
    tidyBtn.textContent = '';
    // Drop the button border while the whirlpool spins — just the spinner,
    // no box around it (restored in the finally below).
    tidyBtn.style.border = 'none';
    tidyBtn.style.background = 'none';
    tidySpinner = spinnerModule.create('', 'clean', 'whirlpool');
    const _spEl = tidySpinner.createElement();
    _spEl.style.position = 'relative';
    _spEl.style.top = '1px';
    tidyBtn.appendChild(_spEl);
    tidySpinner.start();
  }

  // Snapshot current state for diffing
  const beforeMap = new Map(memories.map(m => [m.id, { ...m }]));

  try {
    const res = await fetch(`${window.location.origin}/api/memory/audit`, {
      method: 'POST',
    });

    if (!res.ok) {
      throw new Error((await responseError(res, 'Tidy failed')).message);
    }

    const data = await res.json();
    if (data?.ok !== true) {
      throw new Error(data?.error?.message || 'Tidy did not complete. No memories were changed.');
    }
    if (data.status === 'unchanged') {
      showToast('Already clean');
      return;
    }
    if (data.status !== 'applied') {
      throw new Error('Tidy did not apply any changes.');
    }

    // Fetch the new state
    const freshData = await fetchMemoryPages();
    const afterList = freshData.memory || freshData || [];
    const afterMap = new Map(afterList.map(m => [m.id, m]));

    // Compute diff
    const removed = [];   // IDs that no longer exist
    const edited = [];    // IDs where text changed
    for (const [id, oldMem] of beforeMap) {
      if (!afterMap.has(id)) {
        removed.push(id);
      } else if (afterMap.get(id).text !== oldMem.text) {
        edited.push({ id, oldText: oldMem.text, newText: afterMap.get(id).text });
      }
    }

    if (tidySpinner) tidySpinner.updateMessage('Tidying memories');

    // Animate the diff on the currently rendered list
    await animateTidyDiff(removed, edited);

    // Now load the clean state
    memories = afterList;
    buildCategoryChips();
    renderMemoryList();
    updateMemoryCount();

    const changes = [];
    if (data.updated) changes.push(`${data.updated} edited`);
    if (data.removed) changes.push(`${data.removed} removed`);
    showToast(`Tidied: ${changes.join(', ') || 'changes applied'} (${data.before} \u2192 ${data.after})`);
  } catch (error) {
    console.error('Tidy failed:', error);
    showError(contextualErrorMessage('Tidy failed', error?.message));
  } finally {
    if (tidySpinner) tidySpinner.destroy();
    if (tidyBtn) {
      tidyBtn.disabled = false;
      tidyBtn.style.border = '';
      tidyBtn.style.background = '';
      tidyBtn.innerHTML = uiIcon("sparkles", 11, {"style":"vertical-align:-1px;margin-right:2px;color:var(--accent, var(--red));"}) + " Tidy";
    }
  }
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function animateMemoryRemoval(ids) {
  const idSet = new Set([...ids].map(id => String(id)));
  const memoryList = document.getElementById('memory-list');
  if (!memoryList || !idSet.size) return;
  const items = Array.from(memoryList.querySelectorAll('.memory-item[data-memory-id]'))
    .filter(el => idSet.has(String(el.dataset.memoryId)));
  if (!items.length) return;
  for (const el of items) {
    el.style.maxHeight = `${Math.max(el.getBoundingClientRect().height, el.scrollHeight)}px`;
    el.classList.add('memory-tidy-removing');
  }
  await sleep(520);
}

async function animateTidyDiff(removedIds, editedItems) {
  const memoryList = document.getElementById('memory-list');
  if (!memoryList) return;

  // Tag each rendered item with its memory ID for lookup
  const items = memoryList.querySelectorAll('.memory-item');
  const itemMap = new Map();
  const filtered = getFilteredMemories();
  items.forEach((el, i) => {
    if (filtered[i]) itemMap.set(filtered[i].id, el);
  });

  // Animate edits first — show text morphing
  for (const { id, oldText, newText } of editedItems) {
    const el = itemMap.get(id);
    if (!el) continue;

    const textEl = el.querySelector('.memory-item-text');
    if (!textEl) continue;

    el.classList.add('memory-tidy-editing');
    textEl.classList.add('memory-tidy-text-old');
    await sleep(300);

    textEl.textContent = newText;
    textEl.classList.remove('memory-tidy-text-old');
    textEl.classList.add('memory-tidy-text-new');
    await sleep(400);

    el.classList.remove('memory-tidy-editing');
    textEl.classList.remove('memory-tidy-text-new');
    await sleep(100);
  }

  // Animate removals — strikethrough then fade out
  for (const id of removedIds) {
    const el = itemMap.get(id);
    if (!el) continue;

    el.classList.add('memory-tidy-removing');
    await sleep(200);
  }

  // Let all removals animate together, then wait for them to finish
  if (removedIds.length > 0) {
    await sleep(500);
  }
}

// ---- Filtering helper ----

function getFilteredMemories() {
  const searchTerm = document.getElementById('memory-search')?.value?.toLowerCase().trim() || '';

  let filtered = searchTerm
    ? memories.filter(m => m.text && m.text.toLowerCase().includes(searchTerm))
    : [...memories];

  if (activeCategory !== 'all') {
    filtered = filtered.filter(m => (m.category || 'fact') === activeCategory);
  }

  filtered = filtered.filter(_passesSignalFilters);

  const sortSelect = document.getElementById('memory-sort');
  const sort = sortSelect ? sortSelect.value : sortOrder;
  if (sort === 'newest') {
    filtered.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  } else if (sort === 'oldest') {
    filtered.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  } else if (sort === 'alpha') {
    filtered.sort((a, b) => (a.text || '').localeCompare(b.text || ''));
  } else if (sort === 'uses') {
    filtered.sort((a, b) => (b.uses || 0) - (a.uses || 0) || (b.timestamp || 0) - (a.timestamp || 0));
  }

  // Pinned always float to top
  filtered.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));

  return filtered;
}

// ---- Graph tab (T4: canvas-first) ----

const graphState = {
  nodes: [],
  edges: [],
  positions: new Map(),
  selected: null,
  tagFilter: null,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  wired: false,
  loadId: 0,
  nextTagFilter: null,
  totals: { nodes: 0, edges: 0 },
};

async function _graphApi(params) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`/api/memory/graph?${query}`, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(`graph ${params.op} failed`);
  return response.json();
}

export async function loadMemoryGraph({ tagFilter = null } = {}) {
  const canvas = document.getElementById('memory-graph-canvas');
  if (!canvas) return;
  _wireGraphCanvas(canvas);
  const loadId = ++graphState.loadId;
  try {
    const overview = await _graphApi({ op: 'overview', limit: 120 });
    // A slower, older tab load must not overwrite a newer filtered load.
    if (loadId !== graphState.loadId) return;
    graphState.nodes = overview.nodes || [];
    graphState.edges = overview.edges || [];
    graphState.totals = { nodes: overview.node_total || 0, edges: overview.edge_total || 0 };
    graphState.selected = null;
    graphState.tagFilter = typeof tagFilter === 'string' && tagFilter ? tagFilter : null;
    _resetGraphViewport();
    _relayoutGraph(canvas);
    _renderGraphTags();
    _renderGraphCounts();
    _renderGraphDetail(null);
  } catch (error) {
    if (loadId !== graphState.loadId) return;
    const detail = document.getElementById('memory-graph-detail');
    if (detail) detail.textContent = 'Graph unavailable — is the frankenmemory provider running?';
  }
}

function _resetGraphViewport() {
  graphState.scale = 1;
  graphState.offsetX = 0;
  graphState.offsetY = 0;
}

function _relayoutGraph(canvas) {
  graphState.positions = forceLayout(graphState.nodes, graphState.edges, {
    width: canvas.width, height: canvas.height,
  });
  _drawGraph(canvas);
}

function _graphColors() {
  const styles = getComputedStyle(document.documentElement);
  return {
    fg: styles.getPropertyValue('--fg').trim() || '#ccc',
    accent: (styles.getPropertyValue('--accent') || styles.getPropertyValue('--red')).trim() || '#e06c75',
    border: styles.getPropertyValue('--border').trim() || '#444',
  };
}

const _NODE_KIND_COLORS = {
  person: '#98c379', project: '#61afef', tool: '#d19a66',
  place: '#c678dd', organization: '#56b6c2',
};

function _drawGraph(canvas) {
  const ctx = canvas.getContext('2d');
  const colors = _graphColors();
  ctx.save();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!graphState.nodes.length) {
    ctx.fillStyle = colors.fg;
    ctx.globalAlpha = 0.75;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = '600 13px sans-serif';
    ctx.fillText('No saved memories here yet', canvas.width / 2, canvas.height / 2 - 10);
    ctx.globalAlpha = 0.55;
    ctx.font = '11px sans-serif';
    ctx.fillText(
      'Save or approve a memory and it will appear here.',
      canvas.width / 2,
      canvas.height / 2 + 14,
    );
    ctx.restore();
    return;
  }
  ctx.translate(graphState.offsetX, graphState.offsetY);
  ctx.scale(graphState.scale, graphState.scale);

  const visibleEdges = graphState.tagFilter
    ? graphState.edges.filter((e) => e.tag === graphState.tagFilter)
    : graphState.edges;

  ctx.lineWidth = 1;
  for (const edge of visibleEdges) {
    const a = graphState.positions.get(edge.src_id);
    const b = graphState.positions.get(edge.dst_id);
    if (!a || !b) continue;
    const touchesSelected = graphState.selected
      && (edge.src_id === graphState.selected || edge.dst_id === graphState.selected);
    ctx.strokeStyle = touchesSelected ? colors.accent : colors.border;
    ctx.globalAlpha = touchesSelected ? 0.9 : 0.5;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    if (touchesSelected) {
      ctx.globalAlpha = 0.8;
      ctx.fillStyle = colors.fg;
      ctx.font = '9px sans-serif';
      ctx.fillText(edge.tag, (a.x + b.x) / 2 + 4, (a.y + b.y) / 2 - 2);
    }
  }

  ctx.globalAlpha = 1;
  for (const node of graphState.nodes) {
    const p = graphState.positions.get(node.id);
    if (!p) continue;
    const selected = node.id === graphState.selected;
    ctx.beginPath();
    ctx.arc(p.x, p.y, selected ? 9 : 6, 0, Math.PI * 2);
    ctx.fillStyle = _NODE_KIND_COLORS[node.kind] || colors.accent;
    ctx.fill();
    if (selected) {
      ctx.strokeStyle = colors.fg;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.fillStyle = colors.fg;
    ctx.font = '10px sans-serif';
    ctx.fillText(node.label || node.name, p.x + 10, p.y + 3);
  }
  ctx.restore();
}

function _toGraphCoords(canvas, clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = (clientX - rect.left) * (canvas.width / rect.width);
  const y = (clientY - rect.top) * (canvas.height / rect.height);
  return {
    x: (x - graphState.offsetX) / graphState.scale,
    y: (y - graphState.offsetY) / graphState.scale,
  };
}

function _wireGraphCanvas(canvas) {
  if (graphState.wired) return;
  graphState.wired = true;

  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  let moved = false;

  canvas.addEventListener('pointerdown', (e) => {
    dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = 'grabbing';
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    graphState.offsetX += dx * (canvas.width / canvas.getBoundingClientRect().width);
    graphState.offsetY += dy * (canvas.height / canvas.getBoundingClientRect().height);
    lastX = e.clientX; lastY = e.clientY;
    _drawGraph(canvas);
  });
  canvas.addEventListener('pointerup', async (e) => {
    dragging = false;
    canvas.style.cursor = 'grab';
    if (moved) return; // it was a pan, not a click
    const point = _toGraphCoords(canvas, e.clientX, e.clientY);
    const hit = hitTest(graphState.positions, point.x, point.y, 14 / graphState.scale);
    if (!hit) { graphState.selected = null; _drawGraph(canvas); _renderGraphDetail(null); return; }
    if (e.shiftKey && graphState.selected && graphState.selected !== hit) {
      await _traceBetween(graphState.selected, hit);
      return;
    }
    if (graphState.selected === hit) {
      await _expandNode(hit, canvas);
      return;
    }
    graphState.selected = hit;
    _drawGraph(canvas);
    _renderGraphDetail(hit);
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    graphState.scale = Math.max(0.3, Math.min(4, graphState.scale * factor));
    _drawGraph(canvas);
  }, { passive: false });

  const search = document.getElementById('memory-graph-search');
  if (search) {
    search.addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const query = search.value.trim();
      if (!query) return;
      try {
        const cues = await _graphApi({ op: 'cues', query, limit: 5 });
        const first = (cues.hits || [])[0];
        if (!first) return;
        let node = first.node;
        if (!graphState.nodes.some((n) => n.id === node.id)) {
          graphState.nodes.push(node);
          _relayoutGraph(canvas);
        }
        graphState.selected = node.id;
        const p = graphState.positions.get(node.id);
        if (p) {
          graphState.offsetX = canvas.width / 2 - p.x * graphState.scale;
          graphState.offsetY = canvas.height / 2 - p.y * graphState.scale;
        }
        _drawGraph(canvas);
        _renderGraphDetail(node.id);
      } catch { /* leave canvas as-is */ }
    });
  }
}

async function _expandNode(nodeId, canvas) {
  try {
    const result = await _graphApi({ op: 'expand', node: nodeId, limit: 25 });
    mergeExpansion(graphState, result.hits || []);
    _relayoutGraph(canvas);
    _renderGraphTags();
    _renderGraphDetail(nodeId);
  } catch { /* node stays as-is */ }
}

async function _traceBetween(fromId, toId) {
  const detail = document.getElementById('memory-graph-detail');
  if (!detail) return;
  try {
    const result = await _graphApi({ op: 'trace', node: fromId, to_node: toId, limit: 10 });
    const paths = result.paths || [];
    detail.innerHTML = '';
    const title = document.createElement('div');
    title.style.cssText = 'font-weight:600;margin-bottom:6px;';
    title.textContent = `Paths: ${_nodeName(fromId)} → ${_nodeName(toId)}`;
    detail.appendChild(title);
    if (!paths.length) {
      detail.appendChild(document.createTextNode('No path found.'));
      return;
    }
    for (const path of paths) {
      const row = document.createElement('div');
      row.style.cssText = 'margin-bottom:6px;line-height:1.5;';
      const names = (path.node_ids || []).map(_nodeName);
      const hops = [];
      names.forEach((name, index) => {
        hops.push(name);
        if (path.tags && path.tags[index]) hops.push(`—${path.tags[index]}→`);
      });
      row.textContent = hops.join(' ');
      detail.appendChild(row);
    }
  } catch {
    detail.textContent = 'Trace failed.';
  }
}

function _nodeName(nodeId) {
  const node = graphState.nodes.find((n) => n.id === nodeId);
  return node ? (node.label || node.name) : nodeId;
}

function _renderGraphDetail(nodeId) {
  const detail = document.getElementById('memory-graph-detail');
  if (!detail) return;
  if (!nodeId) {
    const message = graphState.nodes.length
      ? 'Click a node to explore. Click again to expand its neighborhood. Shift-click a second node to trace the paths between them.'
      : 'No saved memories are available in this scope yet.';
    detail.innerHTML = `<span class="admin-toggle-sub" style="margin:0">${message}</span>`;
    return;
  }
  const node = graphState.nodes.find((n) => n.id === nodeId);
  if (!node) return;
  detail.innerHTML = '';
  const title = document.createElement('div');
  title.style.cssText = 'font-weight:600;margin-bottom:4px;';
  title.textContent = node.label || node.name;
  detail.appendChild(title);
  const meta = document.createElement('div');
  meta.style.cssText = 'opacity:0.6;margin-bottom:8px;';
  meta.textContent = `${node.kind} · trust ${node.trust} · seen ${node.last_seen || '?'}`;
  detail.appendChild(meta);
  const edges = graphState.edges.filter((e) => e.src_id === nodeId || e.dst_id === nodeId);
  for (const edge of edges) {
    const row = document.createElement('div');
    row.style.cssText = 'margin-bottom:4px;line-height:1.4;';
    const out = edge.src_id === nodeId;
    const other = _nodeName(out ? edge.dst_id : edge.src_id);
    row.textContent = out ? `${edge.tag} → ${other}` : `← ${edge.tag} ${other}`;
    if (edge.fact) row.title = edge.fact;
    detail.appendChild(row);
  }
  if (!edges.length) {
    const hint = document.createElement('div');
    hint.style.opacity = '0.6';
    hint.textContent = 'No edges loaded — click again to expand.';
    detail.appendChild(hint);
  }
}

function _renderGraphTags() {
  const host = document.getElementById('memory-graph-tags');
  if (!host) return;
  host.innerHTML = '';
  for (const { tag, count } of tagCounts(graphState.edges).slice(0, 8)) {
    const chip = document.createElement('button');
    chip.className = 'memory-cat-chip' + (graphState.tagFilter === tag ? ' active' : '');
    chip.textContent = `${tag} (${count})`;
    chip.addEventListener('click', () => {
      graphState.tagFilter = graphState.tagFilter === tag ? null : tag;
      _renderGraphTags();
      const canvas = document.getElementById('memory-graph-canvas');
      if (canvas) _drawGraph(canvas);
    });
    host.appendChild(chip);
  }
}

function _renderGraphCounts() {
  const el = document.getElementById('memory-graph-counts');
  if (!el) return;
  el.textContent = `showing ${graphState.nodes.length}/${graphState.totals.nodes} nodes · ${graphState.edges.length}/${graphState.totals.edges} edges`;
}

// ---- Digest tab: byte-identical "what the AI sees" ----

export async function loadDigestPreview() {
  const trustedEl = document.getElementById('memory-digest-trusted');
  const untrustedEl = document.getElementById('memory-digest-untrusted');
  const countsEl = document.getElementById('memory-digest-counts');
  const clustersEl = document.getElementById('memory-digest-clusters');
  const editListEl = document.getElementById('memory-digest-edit-list');
  if (!trustedEl || !untrustedEl) return;
  try {
    const response = await fetch('/api/memory/digest-preview', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('digest preview failed');
    const data = await response.json();
    trustedEl.textContent = data.trusted_block || '(nothing endorsed yet — write or pin a memory, or enable the trust toggles)';
    untrustedEl.textContent = data.untrusted_card || '(memory bank is empty)';
    const stamp = document.getElementById('memory-digest-stamp');
    if (stamp) {
      const at = data.digest?.generated_at;
      stamp.textContent = at ? `Generated ${at}.` : '';
    }
    if (countsEl) countsEl.textContent = JSON.stringify(data.digest?.counts || {}, null, 2);
    if (editListEl) _renderDigestEditors(editListEl, data.digest || {});
    if (clustersEl) {
      clustersEl.innerHTML = '';
      const clusters = data.digest?.clusters || [];
      if (!clusters.length) {
        const hint = document.createElement('span');
        hint.className = 'admin-toggle-sub';
        hint.style.margin = '0';
        hint.style.opacity = '0.6';
        hint.textContent = 'No threads yet — clusters appear as the memory graph grows.';
        clustersEl.appendChild(hint);
      }
      for (const cluster of clusters) {
        const chip = document.createElement('button');
        chip.className = 'memory-cat-chip';
        chip.textContent = `${cluster.label} (${cluster.size})`;
        chip.title = 'Open in the graph';
        chip.addEventListener('click', () => {
          // The graph tab lazy-loads its overview. Carry the requested cluster
          // through that async load instead of filtering the stale canvas first.
          graphState.nextTagFilter = cluster.label;
          document.querySelector('.memory-tab[data-memory-tab="graph"]')?.click();
        });
        clustersEl.appendChild(chip);
      }
    }
  } catch (error) {
    trustedEl.textContent = 'Digest unavailable — is the frankenmemory provider running?';
    untrustedEl.textContent = '—';
    if (editListEl) editListEl.replaceChildren();
  }
}

function _categoryForRecord(record) {
  if (record?.category) return record.category;
  const kind = String(record?.kind || '');
  if (kind === 'persona') return 'identity';
  if (kind === 'instruction') return 'preference';
  if (kind === 'unknown') return 'unknown';
  return MEMORY_CATEGORIES.includes(kind) ? kind : 'fact';
}

function _digestEditableRecords(digest) {
  const records = new Map();
  for (const item of digest?.pinned || []) {
    if (!item || !item.id) continue;
    const full = memories.find((memory) => String(memory.id) === String(item.id));
    records.set(String(item.id), {
      ...item,
      ...full,
      id: item.id,
      // `text` is the display projection; raw_text is the stored form the
      // editor must round-trip. Digest entries carry raw_content/raw_headline
      // companions for the same reason.
      text: full?.text || item.content || item.headline || '',
      raw_text: full?.raw_text || full?.text || item.raw_content || item.raw_headline || item.content || item.headline || '',
      category: full?.category || _categoryForRecord(item),
    });
  }
  for (const item of digest?.open_questions || []) {
    if (!item || !item.id) continue;
    const key = String(item.id);
    const full = memories.find((memory) => String(memory.id) === key);
    records.set(key, {
      ...item,
      ...full,
      id: item.id,
      text: full?.text || item.content || '',
      raw_text: full?.raw_text || full?.text || item.raw_content || item.content || '',
      category: full?.category || 'unknown',
    });
  }
  return [...records.values()].filter((record) => record.text);
}

function _renderDigestEditors(host, digest) {
  host.replaceChildren();
  const records = _digestEditableRecords(digest);
  if (!records.length) return;

  const label = document.createElement('div');
  label.className = 'admin-toggle-sub';
  label.style.cssText = 'margin:0 0 6px 0;opacity:0.65';
  label.textContent = 'Edit the source memories surfaced in this digest:';
  host.append(label);

  for (const record of records) {
    const card = document.createElement('div');
    card.className = 'memory-item';
    const text = document.createElement('span');
    text.className = 'memory-item-text';
    text.textContent = record.text;
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'memory-toolbar-btn';
    edit.textContent = 'Edit';
    edit.addEventListener('click', () => _startMemoryCardEditor(card, record, {
      cancel: () => _renderDigestEditors(host, digest),
      refreshDigest: true,
    }));
    card.append(text, edit);
    host.append(card);
  }
}

// ---- Details drawer + signal filters ----

function _detailRow(label, valueNode) {
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;font-size:11px;line-height:1.6;';
  const key = document.createElement('span');
  key.style.cssText = 'opacity:0.6;min-width:92px;flex-shrink:0;';
  key.textContent = label;
  row.appendChild(key);
  if (typeof valueNode === 'string') {
    const value = document.createElement('span');
    value.textContent = valueNode;
    row.appendChild(value);
  } else {
    row.appendChild(valueNode);
  }
  return row;
}

function _buildMemoryDetails(memory) {
  const drawer = document.createElement('div');
  drawer.className = 'memory-details-drawer';
  drawer.style.cssText = 'flex-basis:100%;width:100%;margin-top:6px;padding:8px 10px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:2px;';
  const metadata = memory.metadata || {};

  // Trust is an explicit owner decision, not an inferred confidence score.
  // Keep an unreviewed value blank rather than inventing a midpoint.
  if (memory.source_type) {
    const trustRow = document.createElement('div');
    trustRow.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:11px;line-height:1.6;';
    const trustLabel = document.createElement('span');
    trustLabel.style.cssText = 'opacity:0.6;min-width:92px;flex-shrink:0;';
    trustLabel.textContent = 'Trust';
    trustLabel.title = 'How much do I trust this information?';
    const trustSelect = document.createElement('select');
    trustSelect.className = 'memory-trust-editor';
    trustSelect.title = 'How much do I trust this information?';
    for (const [value, label] of [['unreviewed', 'unreviewed'], ['0.25', 'low'], ['0.5', 'medium'], ['0.75', 'high']]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      trustSelect.appendChild(option);
    }
    const assignment = memory.trust && typeof memory.trust === 'object' ? memory.trust : null;
    if (assignment?.state === 'assigned' && Number.isFinite(Number(assignment.value))) {
      const value = Number(assignment.value);
      trustSelect.value = value < 0.34 ? '0.25' : value < 0.67 ? '0.5' : '0.75';
    } else {
      trustSelect.value = 'unreviewed';
    }
    const saveTrust = document.createElement('button');
    saveTrust.type = 'button';
    saveTrust.className = 'memory-item-btn';
    saveTrust.textContent = 'Save Trust';
    saveTrust.title = 'Append an owner Trust revision';
    saveTrust.addEventListener('click', async () => {
      saveTrust.disabled = true;
      try {
        const selected = trustSelect.value;
        const body = selected === 'unreviewed'
          ? { state: 'unreviewed', reason_code: 'owner_review' }
          : { state: 'assigned', trust: Number(selected), reason_code: 'owner_review' };
        const response = await fetch(`/api/memory/${encodeURIComponent(memory.id)}/trust`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const detail = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(_memoryError(detail, 'Trust update failed'));
        const saved = detail.trust && typeof detail.trust === 'object' ? detail.trust : null;
        memory.trust = saved ? { ...saved, value: saved.value ?? saved.trust ?? null } : null;
        showToast('Trust saved as an owner revision');
        await loadMemories();
      } catch (error) {
        showError(error.message || 'Trust update failed');
      } finally {
        saveTrust.disabled = false;
      }
    });
    trustRow.append(trustLabel, trustSelect, saveTrust);
    drawer.appendChild(trustRow);
  }

  if (Array.isArray(memory.tags) && memory.tags.length) {
    drawer.appendChild(_detailRow('Tags', memory.tags.join(', ')));
  }
  if (memory.scene_name) drawer.appendChild(_detailRow('Scene', memory.scene_name));
  if (memory.source) drawer.appendChild(_detailRow('Source', memory.source));
  if (memory.source_uri) drawer.appendChild(_detailRow('Source URI', memory.source_uri));
  if (memory.source_revision !== null && memory.source_revision !== undefined) {
    drawer.appendChild(_detailRow('Source revision', String(memory.source_revision)));
  }
  if (memory.content_hash) drawer.appendChild(_detailRow('Content hash', memory.content_hash));
  if (memory.provenance_conflict) {
    drawer.appendChild(_detailRow('Provenance', 'Conflicting source revisions need review'));
  }
  if (memory.recall_explanation && typeof memory.recall_explanation === 'object') {
    const explanation = Object.entries(memory.recall_explanation)
      .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
      .map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value}`)
      .join(' · ');
    if (explanation) drawer.appendChild(_detailRow('Why recalled', explanation));
  }
  if (memory.workspace_id && memory.workspace_id !== 'global') {
    drawer.appendChild(_detailRow('Workspace', memory.workspace_id));
  }
  if (memory.workspace_path) drawer.appendChild(_detailRow('Workspace path', memory.workspace_path));
  const flags = [
    memory.archived ? 'archived' : null,
    memory.exempt_from_decay ? 'decay-exempt' : null,
    memory.exempt_from_dedup ? 'dedup-exempt' : null,
  ].filter(Boolean);
  if (flags.length) drawer.appendChild(_detailRow('Flags', flags.join(', ')));

  if (memory.session_id) {
    const link = document.createElement('a');
    link.href = '#';
    link.textContent = memory.session_id;
    link.title = 'Open the chat this memory came from';
    link.style.cssText = 'color:var(--accent,var(--red));text-decoration:underline;';
    link.addEventListener('click', (e) => {
      e.preventDefault();
      import('./sessions.js').then((m) => m.selectSession?.(memory.session_id));
    });
    drawer.appendChild(_detailRow('From chat', link));
  }
  if (Array.isArray(memory.source_message_ids) && memory.source_message_ids.length) {
    drawer.appendChild(_detailRow('Messages', memory.source_message_ids.join(', ')));
  }
  // Authored ledger (wiki records): where the text lives on disk.
  const authored = metadata.authored || metadata.authored_ledger;
  if (authored && typeof authored === 'object') {
    const spot = [authored.path || authored.file, authored.anchor].filter(Boolean).join(' § ');
    if (spot) drawer.appendChild(_detailRow('Authored at', spot));
  }
  if (memory.priority !== null && memory.priority !== undefined) {
    drawer.appendChild(_detailRow('Priority', String(memory.priority)));
  }
  if (memory.last_accessed_at) drawer.appendChild(_detailRow('Last recalled', memory.last_accessed_at));
  if (memory.created_at) drawer.appendChild(_detailRow('Created', memory.created_at));
  if (memory.updated_at) drawer.appendChild(_detailRow('Updated', memory.updated_at));
  const history = document.createElement('div');
  history.className = 'memory-version-history';
  history.appendChild(_detailRow('History', 'Loading…'));
  drawer.appendChild(history);
  fetch(`/api/memory/${encodeURIComponent(memory.id)}/history`, {
    credentials: 'same-origin',
  }).then(async response => {
    const detail = await response.json().catch(() => ({}));
    history.replaceChildren();
    if (!response.ok) {
      history.appendChild(_detailRow('History', _memoryError(detail, 'Unavailable')));
      return;
    }
    history.appendChild(_detailRow(
      'Current',
      `revision ${detail.current_revision} · ${detail.current?.status || 'unknown'}`,
    ));
    for (const revision of (detail.history || []).slice(0, 10)) {
      history.appendChild(_detailRow(
        `Revision ${revision.revision}`,
        `${revision.status} · ${revision.text || '(no value)'}`,
      ));
    }
    if (Array.isArray(detail.evidence)) {
      history.appendChild(_detailRow('Evidence', String(detail.evidence.length)));
    }
    if (Array.isArray(detail.conflicts) && detail.conflicts.length) {
      history.appendChild(_detailRow('Conflicts', String(detail.conflicts.length)));
    }
  }).catch(() => {
    history.replaceChildren(_detailRow('History', 'Unavailable'));
  });
  if (!drawer.childNodes.length) drawer.appendChild(_detailRow('Signals', 'None recorded for this memory.'));
  return drawer;
}

const _SIGNAL_FILTER_DEFS = [
  ['memory-filter-kind', 'kind', ['all', 'instruction', 'persona', 'fact', 'episodic', 'fabric', 'wiki', 'raw', 'unknown']],
  ['memory-filter-provenance', 'provenance', ['all', 'human', 'ai', 'auto_extracted', 'procedural']],
  ['memory-filter-trust', 'trust', ['all', 'trusted', 'reference']],
];

function _syncSignalFilterVisibility() {
  // Signal filters only mean something for the enriched (fm) provider.
  const enriched = memories.some((m) => m.source_type);
  for (const [id, key, options] of _SIGNAL_FILTER_DEFS) {
    const select = document.getElementById(id);
    if (!select) continue;
    select.hidden = !enriched;
    if (!select.options.length) {
      for (const option of options) {
        const el = document.createElement('option');
        el.value = option;
        el.textContent = option === 'all'
          ? `${key}: all`
          : (key === 'kind' && option === 'unknown' ? 'open question' : option.replace('_extracted', ''));
        select.appendChild(el);
      }
      select.addEventListener('change', () => {
        signalFilters[key] = select.value;
        renderMemoryList();
      });
    }
  }
}

function _passesSignalFilters(memory) {
  if (signalFilters.kind !== 'all' && String(memory.kind || '') !== signalFilters.kind) return false;
  if (signalFilters.provenance !== 'all' && String(memory.source_type || '') !== signalFilters.provenance) return false;
  if (signalFilters.trust !== 'all') {
    const trusted = isTrusted(memory, trustPrefsState);
    if (signalFilters.trust === 'trusted' && !trusted) return false;
    if (signalFilters.trust === 'reference' && trusted) return false;
  }
  return true;
}

// ---- Render ----

export function renderMemoryList() {
  const memoryList = document.getElementById('memory-list');
  if (!memoryList) {
    console.error('Memory list element not found');
    return;
  }

  const filtered = getFilteredMemories();
  memoryList.innerHTML = '';

  if (filtered.length === 0) {
    const selectBtn = document.getElementById('memory-select-btn');
    if (selectBtn) selectBtn.disabled = true;
    if (selectMode) exitSelectMode();
    if (memoriesLoading) {
      const row = spinnerModule.createLoadingRow('Loading memories...', 14);
      row.classList.add('memory-empty');
      memoryList.replaceChildren(row);
      return;
    }
    const searchTerm = document.getElementById('memory-search')?.value?.trim() || '';
    const _smiley = '<span style="vertical-align:-3px;margin-left:6px;">' + uiModule.emptyStateIcon('smiley') + '</span>';
    if (memoryLoadError) {
      memoryList.innerHTML = `<div class="memory-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;">
        <span>${uiModule.esc(memoryLoadError)}</span>
        <button type="button" data-memory-retry style="color:var(--accent,var(--red));text-decoration:underline;background:none;border:0;cursor:pointer;">Retry</button>
      </div>`;
      memoryList.querySelector('[data-memory-retry]')?.addEventListener('click', () => loadMemories());
    } else if (searchTerm || activeCategory !== 'all') {
      memoryList.innerHTML = `<div class="memory-empty">No matches.</div>`;
    } else {
      const frankenmemory = memoryProviderStatus === 'frankenmemory';
      memoryList.innerHTML = `<div class="memory-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;">
        <span>${frankenmemory ? 'No curated memories yet' : 'No memories yet'}${_smiley}</span>
        ${frankenmemory ? '<span style="opacity:0.7;font-size:11px;display:block;">Frankenmemory keeps raw evidence separate. <a href="#" data-mem-goto-inspect style="color:var(--accent,var(--red));text-decoration:underline;">Review it in Inspect</a>.</span>' : ''}
        <span style="opacity:0.7;font-size:11px;display:block;">
          <a href="#" data-mem-goto-add style="color:var(--accent,var(--red));text-decoration:underline;">Import in Add tab</a>
        </span>
      </div>`;
      memoryList.querySelector('[data-mem-goto-inspect]')?.addEventListener('click', (e) => {
        e.preventDefault();
        document.querySelector('.memory-tab[data-memory-tab="inspect"]')?.click();
      });
      memoryList.querySelector('[data-mem-goto-add]')?.addEventListener('click', (e) => {
        e.preventDefault();
        document.querySelector('.memory-tab[data-memory-tab="add"]')?.click();
      });
    }
    return;
  }

  const selectBtn = document.getElementById('memory-select-btn');
  if (selectBtn) selectBtn.disabled = false;

  filtered.forEach(memory => {
    const item = document.createElement('div');
    item.className = 'memory-item';
    item.dataset.memoryId = String(memory.id);

    // Checkbox for select mode
    if (selectMode) {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'memory-select-cb';
      cb.checked = selectedIds.has(memory.id);
      cb.addEventListener('change', () => {
        toggleSelectItem(memory.id);
        const selectAllEl = document.getElementById('memory-select-all');
        if (selectAllEl) selectAllEl.checked = filtered.every(m => selectedIds.has(m.id));
      });
      item.appendChild(cb);
      item.style.cursor = 'pointer';
      item.addEventListener('click', (e) => {
        if (e.target === cb) return;
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event('change'));
      });
    }

    // Content: text + metadata
    const content = document.createElement('div');
    content.className = 'memory-item-content';

    const textSpan = document.createElement('span');
    textSpan.className = 'memory-item-text';
    textSpan.textContent = memory.text;

    const meta = document.createElement('div');
    meta.className = 'memory-item-meta';

    if (memory.pinned) {
      const pinBadge = document.createElement('span');
      pinBadge.className = 'memory-cat-badge memory-cat-pinned';
      pinBadge.textContent = 'pinned';
      meta.appendChild(pinBadge);
    }

    const catBadge = document.createElement('span');
    const cat = memory.category || 'fact';
    catBadge.className = 'memory-cat-badge memory-cat-' + cat;
    catBadge.textContent = categoryLabel(cat);
    meta.appendChild(catBadge);

    if (memory.source_type) {
      // Enriched provider record (T2): trust/kind/provenance/score chips
      // from the shared helper, raw values on hover. Rare signals
      // (workspace scope, exemptions, archived) live in the Details
      // drawer instead of the card — chip soup reads as noise.
      const CARD_CHIPS = new Set(['trusted', 'reference', 'kind', 'provenance', 'trust-assignment']);
      for (const chip of memoryChips(memory, trustPrefsState)) {
        const base = chip.cls.split(' ')[0];
        if (!CARD_CHIPS.has(base) && base !== 'score') continue;
        const chipEl = document.createElement('span');
        chipEl.className = 'memory-cat-badge memory-signal-' + base;
        if (base === 'score') chipEl.classList.add('memory-signal-' + chip.cls.split(' ')[1]);
        chipEl.textContent = chip.label;
        chipEl.title = chip.title;
        meta.appendChild(chipEl);
      }
    } else {
      const srcSpan = document.createElement('span');
      srcSpan.className = 'memory-item-source';
      srcSpan.textContent = memory.source === 'auto' ? 'auto' : 'manual';
      meta.appendChild(srcSpan);
    }

    const uses = Number(memory.uses || 0);
    if (uses > 0) {
      const useSpan = document.createElement('span');
      useSpan.className = 'memory-item-uses';
      useSpan.textContent = `${uses}×`;
      useSpan.title = `Injected into chat context ${uses} time${uses === 1 ? '' : 's'}`;
      meta.appendChild(useSpan);
    }

    if (memory.timestamp) {
      const timeSpan = document.createElement('span');
      timeSpan.className = 'memory-item-time';
      timeSpan.textContent = relativeTime(memory.timestamp);
      timeSpan.title = new Date(memory.timestamp * 1000).toLocaleString();
      meta.appendChild(timeSpan);
    }

    content.appendChild(textSpan);
    content.appendChild(meta);

    if (memory.pinned) item.classList.add('memory-pinned');

    item.appendChild(content);

    // Double-click text to edit (not in select mode)
    if (!selectMode) {
      textSpan.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        startInlineEdit(item, memory);
      });
      textSpan.style.cursor = 'text';
    }

    // Menu button (hidden in select mode)
    if (!selectMode) {
      const menuBtn = document.createElement('button');
      menuBtn.className = 'memory-menu-btn';
      menuBtn.innerHTML = uiIcon('more', 14);
      menuBtn.title = 'Actions';

      const dropdown = document.createElement('div');
      dropdown.className = 'memory-item-dropdown';

      // Pin state carries both a bookmark silhouette and a check mark.
      const _pinSvg = uiIcon(memory.pinned ? 'bookmark-filled' : 'bookmark', 14);
      const pinItem = document.createElement('div');
      pinItem.className = 'dropdown-item-compact';
      pinItem.innerHTML = `<span class="dropdown-icon">${_pinSvg}</span><span>${memory.pinned ? 'Unpin' : 'Pin'}</span>`;
      pinItem.addEventListener('click', () => { dropdown.style.display = 'none'; togglePin(memory.id, !memory.pinned); });

      const editItem = document.createElement('div');
      editItem.className = 'dropdown-item-compact';
      editItem.innerHTML = uiIcon('edit', 14) + '<span>Edit</span>';
      editItem.addEventListener('click', () => { dropdown.style.display = 'none'; startInlineEdit(item, memory); });

      // Details drawer (T2): every stored signal reachable — tags, scene,
      // provenance links, authored ledger, access/created/updated times.
      const detailsItem = document.createElement('div');
      detailsItem.className = 'dropdown-item-compact';
      detailsItem.textContent = '☰ Details';
      detailsItem.addEventListener('click', () => {
        dropdown.style.display = 'none';
        const existing = item.querySelector('.memory-details-drawer');
        if (existing) { existing.remove(); return; }
        item.appendChild(_buildMemoryDetails(memory));
        if (item.isConnected && item.getClientRects().length) void recordPresentation('memory.record.opened', {
          recordId: String(memory.id), authorized: true,
        }, { workspaceId: memory.workspace_id || null });
      });

      // Answer open questions by revising their stable block, never deleting it.
      let resolveItem = null;
      if ((memory.kind === 'unknown' || memory.kind === 'open_question' || memory.category === 'unknown') && !memory.archived) {
        resolveItem = document.createElement('div');
        resolveItem.className = 'dropdown-item-compact';
        resolveItem.textContent = '✓ Resolve';
        resolveItem.title = 'Answer this question in the same memory block';
        resolveItem.addEventListener('click', () => {
          dropdown.style.display = 'none';
          resolveQuestion(memory.id);
        });
      }

      const deleteItem = document.createElement('div');
      deleteItem.className = 'dropdown-item-compact memory-dropdown-delete';
      deleteItem.innerHTML = uiIcon('trash', 14) + '<span>Delete</span>';
      deleteItem.addEventListener('click', () => { dropdown.style.display = 'none'; deleteMemory(memory.id); });

      // Select — enters bulk-select mode and pre-selects this memory. Same
      // pattern as the email/documents/skills Select item.
      const selectItem = document.createElement('div');
      selectItem.className = 'dropdown-item-compact';
      selectItem.innerHTML = uiIcon('select', 14) + '<span>Select</span>';
      selectItem.addEventListener('click', (e) => {
        e.stopPropagation();
        if (dropdown.parentNode) dropdown.remove();
        if (!selectMode) enterSelectMode();
        selectedIds.add(memory.id);
        updateBulkCount();
        renderMemoryList();
      });

      // Mobile-only Cancel — mirrors the email/documents popup pattern. CSS
      // hides `.dropdown-cancel-mobile` on desktop where outside-click already
      // dismisses cleanly.
      const cancelItem = document.createElement('div');
      cancelItem.className = 'dropdown-item-compact dropdown-cancel-mobile';
      cancelItem.innerHTML = uiIcon('close', 14) + '<span>Cancel</span>';
      cancelItem.addEventListener('click', (e) => { e.stopPropagation(); if (dropdown.parentNode) dropdown.remove(); });

      dropdown.appendChild(pinItem);
      dropdown.appendChild(selectItem);
      dropdown.appendChild(editItem);
      dropdown.appendChild(detailsItem);
      if (resolveItem) dropdown.appendChild(resolveItem);
      dropdown.appendChild(deleteItem);
      dropdown.appendChild(cancelItem);

      menuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        // Close any other open dropdowns
        document.querySelectorAll('.memory-item-dropdown').forEach(d => d.remove());
        const rect = menuBtn.getBoundingClientRect();
        dropdown.style.position = 'fixed';
        dropdown.style.top = rect.bottom + 2 + 'px';
        dropdown.style.right = (window.innerWidth - rect.right) + 'px';
        dropdown.style.left = 'auto';
        // Portaled to <body>, so it must outrank the Brain modal it belongs to.
        // Tool modals get a monotonically increasing z-index from modalManager's
        // bring-to-front counter, which climbs unbounded over a long session —
        // once it passed the old hardcoded 10001 the menu rendered behind the
        // panel (#4720). topPortalZ() derives the value from the live tool-window
        // stack so the menu always sits just above, however high it has climbed.
        dropdown.style.zIndex = String(topPortalZ());
        dropdown.style.display = 'block';
        document.body.appendChild(dropdown);
        // Keep on-screen (mobile): flip above the button if it overflows the
        // bottom, clamp the left edge, cap height as a last resort.
        const dr = dropdown.getBoundingClientRect();
        if (dr.bottom > window.innerHeight - 6) {
          dropdown.style.top = Math.max(6, rect.top - dr.height - 2) + 'px';
        }
        if (dr.left < 6) {
          dropdown.style.right = Math.max(6, window.innerWidth - 6 - dr.width) + 'px';
        }
        const dr2 = dropdown.getBoundingClientRect();
        if (dr2.bottom > window.innerHeight - 6) {
          dropdown.style.maxHeight = Math.max(80, window.innerHeight - 12 - dr2.top) + 'px';
          dropdown.style.overflowY = 'auto';
        }

        // Swipe-down-to-dismiss — mirrors the documents library popup gesture.
        // Drag the popup down past ~60px and release to close; release earlier
        // and it snaps back. Vertical-only; horizontal flicks fall through.
        let _sw = null;
        let _swDy = 0;
        const _onTS = (ev) => {
          if (ev.touches.length !== 1) return;
          _sw = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
          _swDy = 0;
          dropdown.style.transition = '';
        };
        const _onTM = (ev) => {
          if (!_sw || ev.touches.length !== 1) return;
          const dx = ev.touches[0].clientX - _sw.x;
          const dy = ev.touches[0].clientY - _sw.y;
          if (Math.abs(dy) < Math.abs(dx)) { _sw = null; return; }
          if (dy > 0) {
            _swDy = dy;
            dropdown.style.transform = 'translateY(' + dy + 'px)';
            dropdown.style.opacity = String(Math.max(0.3, 1 - dy / 240));
          }
        };
        const _onTE = () => {
          if (!_sw) return;
          _sw = null;
          if (_swDy > 60) {
            dropdown.style.transition = 'transform 0.15s ease, opacity 0.15s ease';
            dropdown.style.transform = 'translateY(120px)';
            dropdown.style.opacity = '0';
            setTimeout(() => { if (dropdown.parentNode) dropdown.remove(); }, 160);
          } else {
            dropdown.style.transition = 'transform 0.18s ease, opacity 0.18s ease';
            dropdown.style.transform = '';
            dropdown.style.opacity = '';
          }
        };
        dropdown.addEventListener('touchstart', _onTS, { passive: true });
        dropdown.addEventListener('touchmove', _onTM, { passive: true });
        dropdown.addEventListener('touchend', _onTE);
      });

      item.appendChild(menuBtn);

      // Long-press anywhere on the card opens the same dropdown — mirrors the
      // documents library pattern. Skip when the touch starts on the kebab,
      // checkbox, or another button (those have their own click handlers).
      {
        let hold = null;
        let start = null;
        const _lpCancel = () => { if (hold) { clearTimeout(hold); hold = null; } start = null; };
        item.addEventListener('pointerdown', (e) => {
          if (e.target.closest('.memory-menu-btn, .memory-select-cb, button, input')) return;
          start = { x: e.clientX, y: e.clientY };
          hold = setTimeout(() => {
            hold = null;
            item._suppressNextClick = true;
            setTimeout(() => { item._suppressNextClick = false; }, 400);
            if (navigator.vibrate) try { navigator.vibrate(15); } catch {}
            menuBtn.click();
          }, 500);
        });
        item.addEventListener('pointermove', (e) => {
          if (!start) return;
          if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) _lpCancel();
        });
        item.addEventListener('pointerup', _lpCancel);
        item.addEventListener('pointercancel', _lpCancel);
      }

      // Close dropdown on outside click
      document.addEventListener('click', () => { if (dropdown.parentNode) dropdown.remove(); }, { once: false });
    }

    memoryList.appendChild(item);
  });

}

// ---- Inline edit with category picker ----

function startInlineEdit(item, memory) {
  item.innerHTML = '';
  item.className = 'memory-item memory-item-editing';

  const editRow = document.createElement('div');
  editRow.className = 'memory-edit-row';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'memory-item-edit-input';
  // Edit buffers always start from the stored (raw) text; `text` may be the
  // identity-rendered display projection.
  input.value = memory.raw_text || memory.text;

  const catSelect = document.createElement('select');
  catSelect.className = 'memory-edit-cat-select';
  MEMORY_CATEGORIES.forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat;
    opt.textContent = categoryLabel(cat);
    if (cat === (memory.category || 'fact')) opt.selected = true;
    catSelect.appendChild(opt);
  });

  editRow.appendChild(input);
  editRow.appendChild(catSelect);

  const actions = document.createElement('div');
  actions.className = 'memory-item-actions';
  actions.style.opacity = '1';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'memory-item-btn save';
  saveBtn.textContent = 'save';
  saveBtn.addEventListener('click', () => saveInlineEdit(memory.id, input.value, catSelect.value));

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'memory-item-btn';
  cancelBtn.textContent = 'cancel';
  cancelBtn.addEventListener('click', () => renderMemoryList());

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);

  item.appendChild(editRow);
  item.appendChild(actions);

  input.focus();
  input.select();

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveInlineEdit(memory.id, input.value, catSelect.value);
    if (e.key === 'Escape') {
      e.stopPropagation();
      e.stopImmediatePropagation();
      renderMemoryList();
    }
  });
}

function _startMemoryCardEditor(card, memory, {
  cancel,
  refreshInspect = false,
  refreshDigest = false,
} = {}) {
  card.replaceChildren();
  card.className = 'memory-item memory-item-editing';

  const editRow = document.createElement('div');
  editRow.className = 'memory-edit-row';
  const input = document.createElement('textarea');
  input.className = 'memory-item-edit-input';
  input.rows = 3;
  // Edit buffers always start from the stored (raw) text; `text` may be the
  // identity-rendered display projection.
  input.value = memory.raw_text || memory.text || '';
  const category = document.createElement('select');
  category.className = 'memory-edit-cat-select';
  for (const value of MEMORY_CATEGORIES) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = categoryLabel(value);
    option.selected = value === _categoryForRecord(memory);
    category.append(option);
  }
  editRow.append(input, category);

  const actions = document.createElement('div');
  actions.className = 'memory-item-actions';
  actions.style.opacity = '1';
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'memory-item-btn save';
  save.textContent = 'save';
  save.addEventListener('click', async () => {
    save.disabled = true;
    const updated = await saveInlineEdit(
      memory.id,
      input.value,
      category.value,
      { refreshInspect, refreshDigest, currentMemory: memory },
    );
    if (!updated) save.disabled = false;
  });
  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.className = 'memory-item-btn';
  cancelButton.textContent = 'cancel';
  cancelButton.addEventListener('click', () => cancel?.());
  actions.append(save, cancelButton);
  card.append(editRow, actions);
  input.focus();
  input.select();
}

async function saveInlineEdit(id, newText, newCategory, {
  refreshInspect = false,
  refreshDigest = false,
  currentMemory = null,
} = {}) {
  newText = newText.trim();
  if (!newText) return false;

  const memory = memories.find(m => String(m.id) === String(id))
    || inspectedMemories.find(m => String(m.id) === String(id))
    || currentMemory;
  const catChanged = newCategory && newCategory !== (memory?.category || 'fact');
  const storedText = memory ? (memory.raw_text || memory.text) : null;
  if (!memory || (newText === storedText && !catChanged)) {
    if (refreshInspect) renderMemoryInspect();
    else if (!refreshDigest) renderMemoryList();
    return true;
  }

  try {
    const params = new URLSearchParams({ text: newText });
    if (newCategory) params.append('category', newCategory);

    const response = await fetch(`${window.location.origin}/api/memory/${id}`, {
      method: 'PUT',
      credentials: 'same-origin',
      body: params
    });

    if (response.ok) {
      await loadMemories();
      if (refreshInspect) await loadMemoryInspect();
      if (refreshDigest) await loadDigestPreview();
      showToast('Memory updated');
      return true;
    } else {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || 'Failed to update memory');
    }
  } catch (error) {
    console.error('Error updating memory:', error);
    showError(error.message || 'Failed to update memory');
    return false;
  }
}

export function updateMemoryCount() {
  const h2Count = document.getElementById('memory-count-h2');
  const tabCount = document.getElementById('memory-count'); // optional (may be absent)
  if (!h2Count && !tabCount) return;
  if (memoriesLoading) {
    if (h2Count) h2Count.textContent = 'loading...';
    if (tabCount) tabCount.textContent = '...';
    return;
  }

  const searchInput = document.getElementById('memory-search');
  const searchTerm = searchInput ? searchInput.value.toLowerCase().trim() : '';

  let visible = memories;
  const scopeTotal = visible.length;
  if (searchTerm) {
    visible = visible.filter(m => m.text && m.text.toLowerCase().includes(searchTerm));
  }
  if (activeCategory !== 'all') {
    visible = visible.filter(m => (m.category || 'fact') === activeCategory);
  }

  const num = visible.length === scopeTotal ? `${scopeTotal}` : `${visible.length}/${scopeTotal}`;
  // Header (next to the "Memories" title) reads "N memories", like the
  // Documents header. The bare number still feeds any tab badge if present.
  if (h2Count) h2Count.textContent = `${num} ${scopeTotal === 1 && visible.length === scopeTotal ? 'memory' : 'memories'}`;
  if (tabCount) tabCount.textContent = num;
}

export async function addNewMemory() {
  const input = document.getElementById('new-memory-input');
  const text = input.value.trim();
  const category = _readNewMemoryCategory();

  if (!text) {
    showError('Memory text cannot be empty');
    return;
  }

  try {
    const response = await fetch(`${window.location.origin}/api/memory/add`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        text: text,
        category: category,
      })
    });

    if (response.ok) {
      const result = await response.json();
      input.value = '';
      await loadMemories();
      showToast(result.pending_review ? 'Sent to Candidates for review' : 'Memory added');
    } else {
      const errorData = await response.json();
      console.error('Server error details:', errorData);
      throw new Error(errorData.detail || 'Failed to add memory');
    }
  } catch (error) {
    console.error('Error adding memory:', error);
    showError(error.message || 'Failed to add memory');
  }
}

export async function editMemory(id) {
  const memory = memories.find(m => m.id === id);
  if (!memory) return;

  const storedText = memory.raw_text || memory.text;
  const newText = prompt('Edit memory:', storedText);
  if (!newText || newText === storedText) return;

  await saveInlineEdit(id, newText);
}

async function resolveQuestion(id) {
  const answer = prompt('Answer this open question:');
  if (!answer?.trim()) return;
  try {
    let expectedRevision = '';
    const history = await fetch(`/api/memory/${encodeURIComponent(id)}/history`, {
      credentials: 'same-origin',
    });
    if (history.ok) {
      const detail = await history.json();
      expectedRevision = String(detail.current_revision || '');
    }
    const form = new URLSearchParams({ answer: answer.trim() });
    if (expectedRevision) form.set('expected_revision', expectedRevision);
    const res = await fetch(`${window.location.origin}/api/memory/${id}/resolve`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    });
    if (res.ok) {
      await loadMemories();
      showToast('Question answered in the same memory');
    } else {
      const err = await res.json().catch(() => ({}));
      showError(_memoryError(err, 'Failed to resolve question'));
    }
  } catch (e) {
    console.error('Failed to resolve question:', e);
    showError('Failed to resolve question');
  }
}

async function togglePin(id, pinned) {
  try {
    const res = await fetch(`${window.location.origin}/api/memory/${id}/pin`, {
      method: 'POST',
      body: new URLSearchParams({ pinned: pinned.toString() })
    });
    if (res.ok) {
      const mem = memories.find(m => m.id === id);
      if (mem) mem.pinned = pinned;
      renderMemoryList();
      showToast(pinned ? 'Pinned — always in context' : 'Unpinned — RAG only');
    }
  } catch (e) {
    console.error('Failed to toggle pin:', e);
    showError('Failed to update pin');
  }
}

export async function deleteMemory(id) {
  const memory = memories.find(m => m.id === id);
  if (!memory) return;

  if (!await uiModule.styledConfirm(`Delete this memory?\n"${memory.text}"`, { confirmText: 'Delete', danger: true })) return;

  try {
    const response = await fetch(`${window.location.origin}/api/memory/${id}`, {
      method: 'DELETE'
    });

    if (response.ok) {
      await animateMemoryRemoval([id]);
      await loadMemories();
      showToast('Memory deleted');
    } else {
      throw new Error('Failed to delete');
    }
  } catch (error) {
    showError('Failed to delete memory');
  }
}

export async function extractMemory(sessionId) {
  const res = await fetch(`${window.location.origin}/api/memory/extract`, {
    method: 'POST',
    body: new URLSearchParams({ session: sessionId })
  });
  if (!res.ok) {
    showError('Failed to extract memory suggestions');
    return;
  }
  const data = await res.json();
  const suggestions = data.suggestions || [];

  const modal = document.getElementById('memory-modal');
  const body = document.getElementById('memory-suggestions-body');
  if (!body) {
    console.error('memory-suggestions-body element not found');
    return;
  }

  body.innerHTML = '';
  body.classList.remove('hidden');

  const memList = document.getElementById('memory-list');
  if (memList) memList.classList.add('hidden');

  if (suggestions.length === 0) {
    body.innerHTML = '<div class="memory-empty">No useful information detected.</div>';
  } else {
    const header = document.createElement('div');
    header.className = 'memory-suggestions-header';
    header.innerHTML = '<span>Suggested memories</span>';
    const backBtn = document.createElement('button');
    backBtn.className = 'memory-item-btn';
    backBtn.textContent = 'back';
    backBtn.addEventListener('click', () => {
      body.classList.add('hidden');
      body.innerHTML = '';
      if (memList) memList.classList.remove('hidden');
    });
    header.appendChild(backBtn);
    body.appendChild(header);

    suggestions.forEach(s => {
      const div = document.createElement('div');
      div.className = 'memory-suggestion-item';
      const txt = document.createElement('span');
      txt.className = 'memory-item-text';
      txt.textContent = s;
      const btn = document.createElement('button');
      btn.className = 'memory-item-btn save';
      btn.textContent = 'save';
      btn.addEventListener('click', async () => {
        try {
          const response = await fetch(`${window.location.origin}/api/memory/add`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: s })
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || 'Failed to save memory');
          btn.disabled = true;
          btn.textContent = result.pending_review ? 'in review' : 'saved';
          showToast(result.pending_review ? 'Sent to Candidates for review' : 'Saved to memory');
        } catch (error) {
          console.error('Failed to save suggested memory:', error);
          showError(error.message || 'Failed to save memory');
        }
      });
      div.appendChild(txt);
      div.appendChild(btn);
      body.appendChild(div);
    });
  }

  modal.classList.remove('hidden');
}

// ---- Export ----

// EC-D04: the Export button opens a dialog that assembles the S00 filter
// query; untouched defaults still download today's full v3 bundle.
let _memoryExportDialog = null;

export function exportMemories() {
  if (!_memoryExportDialog) {
    _memoryExportDialog = createMemoryExportDialog({
      document,
      host: document.body,
      fetchImpl: fetch,
      urlApi: URL,
      showToast: (message, opts) => showToast(message, opts),
      showError: (message, opts) => showError(message, opts),
    });
  }
  _memoryExportDialog.open();
}

// EC-D05: single owner-scoped photo download through the S00 asset endpoint.
export async function downloadMemoryPhoto(assetId, filename) {
  await downloadMemoryAsset({
    document,
    fetchImpl: fetch,
    urlApi: URL,
    showToast: (message, opts) => showToast(message, opts),
    showError: (message, opts) => showError(message, opts),
  }, assetId, filename);
}

// ---- Import from file ----

export async function importMemories() {
  const fileInput = document.getElementById('memory-import-file');
  if (!fileInput) return;
  fileInput.click();
}

function _memoryBindingIdempotencyKey() {
  const suffix = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `memory-import-route-${suffix}`;
}

async function _configureMemoryRoute(problem, route, file) {
  const files = Array.isArray(file) ? file : [file];
  const routeId = route?.model_route_id;
  if (!routeId) throw new Error('The selected Memory model route is no longer available.');
  showToast(`Configuring ${route.display_name || 'the selected model'} for Memory…`, {
    duration: 15000,
    leadingIcon: 'spinner',
  });
  const response = await fetch('/api/v1/providers/bindings/memory', {
    method: 'PUT',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'If-Match': `"${Number(problem.binding_revision || 0)}"`,
      'Idempotency-Key': _memoryBindingIdempotencyKey(),
    },
    body: JSON.stringify({
      routes: [{ model_route_id: routeId, enabled: true }],
    }),
  });
  if (!response.ok) {
    throw new Error((await responseError(response, 'Memory model setup failed')).message);
  }
  pendingMemoryImportFile = null;
  pendingMemoryImportFiles = [];
  document.dispatchEvent(new CustomEvent('open-clank:providers-updated'));
  showToast(`${route.display_name || 'Model'} is now the Memory model. Retrying import…`, {
    duration: 5000,
    leadingIcon: 'check',
  });
  await handleImportFiles(files);
}

async function _openMemoryRouteSettings(problem, file) {
  const files = Array.isArray(file) ? file : [file];
  pendingMemoryImportFiles = files.filter(Boolean);
  pendingMemoryImportFile = pendingMemoryImportFiles[0] || null;
  const settingsModule = await import('./settings.js');
  settingsModule.open(problem.settings_target || 'added-models');
  showToast('Choose and save a Memory model route; this file will retry automatically.', {
    duration: 12000,
  });
}

function _showMemoryRouteProblem(problem, file, detail) {
  const files = Array.isArray(file) ? file : [file];
  pendingMemoryImportFiles = files.filter(Boolean);
  pendingMemoryImportFile = pendingMemoryImportFiles[0] || null;
  const routes = Array.isArray(problem.eligible_routes) ? problem.eligible_routes : [];
  const only = routes.length === 1 ? routes[0] : null;
  const label = only?.display_name || only?.model_id || 'available model';
  showError(contextualErrorMessage('Import failed', detail), {
    duration: 20000,
    action: only ? `Use ${label}` : 'Open model settings',
    actionHint: only
      ? `Set ${label} as the dedicated Memory model and retry this file`
      : 'Choose a dedicated Memory model and retry this file',
    onAction: () => only
      ? _configureMemoryRoute(problem, only, file)
      : _openMemoryRouteSettings(problem, file),
  });
}

// ── Import batch recovery ────────────────────────────────────────────────
// The import POST is long-running on purpose (one extraction per file). A
// proxy read timeout can sever that response while the durable batch keeps
// processing server-side. These helpers rediscover unresolved batches via
// the owner-scoped list endpoint so review is never stranded.

async function _fetchPendingImportBatches() {
  try {
    const response = await fetch('/api/memory/import-batches', { credentials: 'same-origin' });
    if (!response.ok) return [];
    const data = await response.json();
    return Array.isArray(data?.batches) ? data.batches : [];
  } catch (error) {
    console.warn('Pending import batch check failed:', error);
    return [];
  }
}

function _importBatchReviewPending(batch) {
  const reviewCounts = batch?.review_counts && typeof batch.review_counts === 'object'
    ? batch.review_counts
    : {};
  return Number(reviewCounts.pending || 0) > 0;
}

async function _waitForImportBatch(batchId, { timeoutMs = 300000, intervalMs = 3000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(
        `/api/memory/import-batches/${encodeURIComponent(batchId)}`,
        { credentials: 'same-origin' },
      );
      if (response.ok) {
        last = await response.json();
        if (String(last?.state || '') !== 'active') return last;
      }
    } catch (error) {
      console.warn('Import batch status poll failed:', error);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return last;
}

async function _openImportBatchReview(batchId) {
  const data = await _waitForImportBatch(batchId);
  if (!data) return false;
  _renderBatchReview(data);
  _refreshPendingImportNotice();
  return true;
}

async function _recoverImportBatchAfterTimeout() {
  showToast('Import is still processing — watching for it to finish…');
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline) {
    const batches = await _fetchPendingImportBatches();
    // Only recover a batch the server touched recently; a stale pending
    // batch from an earlier session is resumed via the notice, not here.
    const latest = batches.find((batch) => {
      const updated = Date.parse(String(batch?.updated_at || ''));
      return batch?.batch_id && Number.isFinite(updated) && Date.now() - updated < 15 * 60 * 1000;
    });
    if (latest) {
      return _openImportBatchReview(latest.batch_id);
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  return false;
}

async function _refreshPendingImportNotice() {
  const notice = document.getElementById('memory-import-pending');
  if (!notice) return;
  const batches = await _fetchPendingImportBatches();
  const actionable = batches.find((batch) => String(batch?.state) === 'active')
    || batches.find((batch) => _importBatchReviewPending(batch));
  if (!actionable) {
    notice.classList.add('hidden');
    notice.replaceChildren();
    return;
  }
  notice.classList.remove('hidden');
  notice.replaceChildren();
  const label = document.createElement('span');
  label.className = 'memory-import-pending-text';
  const files = Array.isArray(actionable.filenames) && actionable.filenames.length
    ? ` (${actionable.filenames.join(', ')})`
    : '';
  if (String(actionable.state) === 'active') {
    label.textContent = `Import still processing${files} — review opens when it finishes…`;
    notice.appendChild(label);
    _waitForImportBatch(actionable.batch_id).then((data) => {
      if (data && String(data.state) !== 'active') showToast('Import finished — ready for review');
      _refreshPendingImportNotice();
    });
    return;
  }
  const pending = Number(actionable.review_counts?.pending || 0);
  label.textContent = `${pending} imported suggestion${pending === 1 ? '' : 's'} waiting for review${files}`;
  const review = document.createElement('button');
  review.className = 'memory-item-btn save';
  review.textContent = 'review';
  review.addEventListener('click', () => _openImportBatchReview(actionable.batch_id));
  const dismiss = document.createElement('button');
  dismiss.className = 'memory-item-btn';
  dismiss.textContent = 'dismiss';
  dismiss.addEventListener('click', async () => {
    // Dismiss the whole queue, not just the batch the banner happens to
    // show: a pre-fix nuke can leave several awaiting_review batches behind.
    const ok = await dismissAllPendingImportBatches({
      fetchImpl: fetch,
      origin: window.location.origin,
      showToast,
      showError,
    });
    if (ok) await _refreshPendingImportNotice();
  });
  notice.append(label, review, dismiss);
}

function _batchReviewItems(data) {
  const items = [];
  const batchId = String(data?.batch_id || '');
  for (const item of Array.isArray(data?.items) ? data.items : []) {
    const suggestions = item?.result?.suggestions;
    if (!Array.isArray(suggestions)) continue;
    for (const suggestion of suggestions) {
      const text = typeof suggestion === 'string' ? suggestion : suggestion?.text;
      if (!text) continue;
      const review = typeof suggestion === 'object'
        && suggestion?.review
        && typeof suggestion.review === 'object'
        ? suggestion.review
        : null;
      const reviewState = String(review?.state || 'awaiting_review');
      items.push({
        batchId,
        suggestionId: (typeof suggestion === 'object' && suggestion?.suggestion_id) || null,
        text: String(text),
        category: (typeof suggestion === 'object' && suggestion?.category) || 'fact',
        filename: item.filename || 'upload',
        itemId: item.item_id || null,
        questionContext: (typeof suggestion === 'object' && suggestion?.question_context) || null,
        review,
        reviewState,
        reviewKey: null,
        active: !['accepted', 'reused', 'rejected'].includes(reviewState),
      });
    }
  }
  return items;
}

function _batchPhotoItems(data) {
  return (Array.isArray(data?.items) ? data.items : [])
    .filter((item) => item?.result?.media?.asset_id)
    .map((item) => ({
      assetId: String(item.result.media.asset_id),
      filename: String(item.result.media.filename || item.filename || 'photo'),
      associatedText: String(item.result.associated_text || '').trim(),
    }));
}

function _batchReviewIdempotencyKey(item) {
  if (!item.reviewKey) {
    const suffix = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    item.reviewKey = `memory-import-review-${item.suggestionId || item.itemId || 'unknown'}-${suffix}`;
  }
  return item.reviewKey;
}

async function _submitBatchReview(item, action) {
  if (!item.batchId || !item.suggestionId) {
    throw new Error('This imported suggestion is missing its review receipt. Re-upload the source file.');
  }
  const proposal = {
    text: String(item.text || '').trim(),
    category: String(item.category || 'fact').trim().toLowerCase(),
  };
  if (item.questionContext) proposal.question_context = item.questionContext;
  const key = _batchReviewIdempotencyKey(item);
  const response = await fetch(
    `${window.location.origin}/api/memory/import-batches/${encodeURIComponent(item.batchId)}/review`,
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': key,
      },
      body: JSON.stringify({
        suggestion_id: item.suggestionId,
        action,
        idempotency_key: key,
        proposal,
      }),
    },
  );
  if (!response.ok) {
    throw new Error((await responseError(response, 'Imported-memory review failed')).message);
  }
  const result = await response.json();
  item.review = result?.review?.review || result?.review || item.review;
  item.reviewState = String(item.review?.state || action);
  item.active = !['accepted', 'reused', 'rejected'].includes(item.reviewState);
  return result;
}

function _appendBatchItemError(card, message) {
  const existing = card.querySelector('.memory-import-error');
  const errorText = existing || document.createElement('small');
  errorText.textContent = message;
  errorText.className = 'memory-import-error';
  if (!existing) card.appendChild(errorText);
}

function _batchFailedItems(data) {
  return (Array.isArray(data?.items) ? data.items : [])
    .filter((item) => item?.state === 'failed_terminal')
    .map((item) => ({
      batchId: String(data?.batch_id || ''),
      itemId: String(item?.item_id || ''),
      filename: String(item?.filename || 'upload'),
      error: item?.error && typeof item.error === 'object'
        ? String(item.error.message || 'This file could not be imported.')
        : 'This file could not be imported.',
    }))
    .filter((item) => item.batchId && item.itemId);
}

function _renderBatchReview(data) {
  const body = document.getElementById('memory-suggestions-body');
  const modal = document.getElementById('memory-modal');
  const memList = document.getElementById('memory-list');
  if (!body || !modal) return;
  const reviewItems = _batchReviewItems(data);
  const failedItems = _batchFailedItems(data);
  body.innerHTML = '';
  body.classList.remove('hidden');
  if (memList) memList.classList.add('hidden');
  if (reviewItems.length === 0 && failedItems.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'memory-empty';
    empty.textContent = 'No useful information found in the selected files.';
    body.appendChild(empty);
    modal.classList.remove('hidden');
    return;
  }

  const header = document.createElement('div');
  header.className = 'memory-suggestions-header';
  const title = document.createElement('span');
  const updateTitle = () => {
    const remaining = reviewItems.filter((item) => item.active).length;
    const failed = failedItems.length;
    title.textContent = failed
      ? `Imported ${reviewItems.length} suggestions (${remaining} remaining; ${failed} file${failed === 1 ? '' : 's'} need attention)`
      : `Imported ${reviewItems.length} suggestions (${remaining} remaining)`;
  };
  updateTitle();
  const actions = document.createElement('div');
  actions.className = 'memory-suggestions-actions';
  const saveAll = document.createElement('button');
  saveAll.className = 'memory-item-btn save';
  saveAll.textContent = 'save all';
  saveAll.addEventListener('click', async () => {
    saveAll.disabled = true;
    let saved = 0;
    let failed = 0;
    for (const item of reviewItems) {
      if (!item.active || !item.text) continue;
      try {
        await _submitBatchReview(item, 'accept');
        saved += 1;
      } catch (error) {
        item.error = error?.message || 'Failed to save memory';
        failed += 1;
      }
    }
    updateTitle();
    saveAll.disabled = false;
    if (saved) await loadMemories();
    showToast(failed ? `Saved ${saved}; ${failed} still need attention` : `Saved ${saved} imported memories`);
  });
  const back = document.createElement('button');
  back.className = 'memory-item-btn';
  back.textContent = 'back';
  back.addEventListener('click', () => {
    body.classList.add('hidden');
    body.innerHTML = '';
    if (memList) memList.classList.remove('hidden');
  });
  actions.append(saveAll, back);
  header.append(title, actions);
  body.appendChild(header);

  // EC-D05: photo imports carry the admitted asset in item.result.media —
  // render each photo with a per-photo download button hitting the S00
  // single-asset endpoint (thumbnails stream from the same owner-scoped URL).
  for (const photo of _batchPhotoItems(data)) {
    const card = document.createElement('div');
    card.className = 'memory-suggestion-item memory-photo-item';
    const thumb = document.createElement('img');
    thumb.className = 'memory-photo-thumb';
    thumb.src = memoryAssetUrl(photo.assetId);
    thumb.alt = photo.filename;
    thumb.loading = 'lazy';
    const content = document.createElement('div');
    content.className = 'memory-item-content';
    const source = document.createElement('small');
    source.className = 'memory-import-source';
    source.textContent = photo.filename;
    content.appendChild(source);
    if (photo.associatedText) {
      const text = document.createElement('small');
      text.className = 'memory-item-text';
      text.textContent = photo.associatedText;
      content.appendChild(text);
    }
    const controls = document.createElement('div');
    controls.className = 'memory-suggestion-actions';
    const download = document.createElement('button');
    download.className = 'memory-item-btn save';
    download.textContent = 'download';
    download.addEventListener('click', async () => {
      download.disabled = true;
      try {
        await downloadMemoryPhoto(photo.assetId, photo.filename);
      } finally {
        download.disabled = false;
      }
    });
    controls.appendChild(download);
    card.append(thumb, content, controls);
    body.appendChild(card);
  }

  for (const item of reviewItems) {
    if (!item.active) continue;
    const card = document.createElement('div');
    card.className = 'memory-suggestion-item';
    const content = document.createElement('div');
    content.className = 'memory-item-content';
    const source = document.createElement('small');
    source.textContent = item.filename;
    source.className = 'memory-import-source';
    const text = document.createElement('textarea');
    text.className = 'memory-item-edit-input';
    text.rows = 2;
    text.value = item.text;
    text.setAttribute('aria-label', `Imported memory text from ${item.filename}`);
    text.addEventListener('input', () => { item.text = text.value.trim(); });
    const category = document.createElement('select');
    category.className = 'memory-edit-cat-select';
    category.setAttribute('aria-label', `Imported memory category from ${item.filename}`);
    for (const value of MEMORY_CATEGORIES) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = categoryLabel(value);
      option.selected = value === item.category;
      category.appendChild(option);
    }
    category.addEventListener('change', () => { item.category = category.value; });
    let about = null;
    if (item.category === 'unknown') {
      about = document.createElement('select');
      about.className = 'memory-question-about-select';
      about.setAttribute('aria-label', `Question subject from ${item.filename}`);
      const none = document.createElement('option');
      none.value = '';
      none.textContent = 'unassociated question';
      about.appendChild(none);
      for (const principal of (data?.principal_context?.principals || [])) {
        const option = document.createElement('option');
        option.value = JSON.stringify({ kind: principal.kind, id: principal.id });
        option.textContent = `about ${principal.label}`;
        about.appendChild(option);
      }
      about.addEventListener('change', () => {
        item.questionContext = about.value
          ? { mode: 'missing_slot', target: JSON.parse(about.value) }
          : null;
      });
    }
    content.append(source, text, category);
    if (about) content.appendChild(about);
    const controls = document.createElement('div');
    controls.className = 'memory-suggestion-actions';
    const save = document.createElement('button');
    save.className = 'memory-item-btn save';
    save.textContent = 'save';
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const result = await _submitBatchReview(item, 'accept');
        card.remove();
        updateTitle();
        await loadMemories();
        const outcome = result?.review?.review?.state || result?.review?.state;
        showToast(outcome === 'reused' ? 'Already represented in Memory; kept this source receipt' : 'Saved to memory');
      } catch (error) {
        item.error = error?.message || 'Failed to save memory';
        _appendBatchItemError(card, item.error);
        showError(item.error);
      } finally {
        if (item.active) save.disabled = false;
      }
    });
    const remove = document.createElement('button');
    remove.className = 'memory-item-btn delete';
    remove.textContent = 'delete';
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        await _submitBatchReview(item, 'reject');
        card.remove();
        updateTitle();
        showToast('Import suggestion rejected');
      } catch (error) {
        item.error = error?.message || 'Failed to reject import suggestion';
        _appendBatchItemError(card, item.error);
        showError(item.error);
        remove.disabled = false;
      }
    });
    controls.append(save, remove);
    card.append(content, controls);
    body.appendChild(card);
  }

  for (const item of failedItems) {
    const card = document.createElement('div');
    card.className = 'memory-suggestion-item memory-import-failed-item';
    const content = document.createElement('div');
    content.className = 'memory-item-content';
    const source = document.createElement('small');
    source.className = 'memory-import-source';
    source.textContent = item.filename;
    const reason = document.createElement('small');
    reason.className = 'memory-import-error';
    reason.textContent = item.error;
    content.append(source, reason);
    const controls = document.createElement('div');
    controls.className = 'memory-suggestion-actions';
    const retry = document.createElement('button');
    retry.className = 'memory-item-btn save';
    retry.textContent = 'retry file';
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      try {
        const form = new FormData();
        form.append('item_id', item.itemId);
        if (data?.session_id) form.append('session', String(data.session_id));
        const response = await fetch(
          `${window.location.origin}/api/memory/import-batches/${encodeURIComponent(item.batchId)}/retry`,
          { method: 'POST', credentials: 'same-origin', body: form },
        );
        if (!response.ok) throw new Error((await responseError(response, 'Import retry failed')).message);
        _renderBatchReview(await response.json());
      } catch (error) {
        _appendBatchItemError(card, error?.message || 'Import retry failed');
        retry.disabled = false;
      }
    });
    controls.appendChild(retry);
    card.append(content, controls);
    body.appendChild(card);
  }
  modal.classList.remove('hidden');
}

async function handleImportFiles(files) {
  const selected = Array.from(files || []).filter(Boolean);
  if (!selected.length) return;
  // Every interactive import uses the durable batch/review authority, even
  // for one text file. The legacy route remains an API compatibility shim,
  // but its client-side /add flow cannot preserve server-owned provenance or
  // proposed entity matches.
  const sessionId = sessionModule?.getCurrentSessionId?.();
  const importBtn = document.getElementById('memory-import-btn');
  const original = importBtn ? importBtn.innerHTML : '';
  let spin = null;
  if (importBtn) {
    importBtn.disabled = true;
    importBtn.innerHTML = '';
    spin = spinnerModule.createWhirlpool(12);
    spin.element.style.cssText = 'width:12px;height:12px;margin:0 5px 0 0;display:inline-flex;vertical-align:-2px;';
    importBtn.append(spin.element, document.createTextNode(`Importing ${selected.length}`));
  }
  try {
    const formData = new FormData();
    for (const file of selected) formData.append('files', file);
    if (sessionId) formData.append('session', sessionId);
    const response = await fetch(`${window.location.origin}/api/memory/import-batches`, {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      const failure = await responseError(response, 'Import failed');
      const error = new Error(failure.message);
      error.problem = failure.problem;
      throw error;
    }
    pendingMemoryImportFiles = [];
    pendingMemoryImportFile = null;
    const data = await response.json();
    _renderBatchReview(data);
  } catch (error) {
    console.error('Batch import failed:', error);
    if (error?.problem?.code === 'MEMORY_ROUTE_UNCONFIGURED') {
      _showMemoryRouteProblem(error.problem, selected, error.message);
    } else {
      // A proxy read timeout can sever the response while the durable batch
      // keeps processing server-side; recover it instead of lying "failed".
      // Structured server rejections (413/400 problems) never created a
      // batch, so only unstructured failures are worth recovering.
      const recovered = error?.problem ? false : await _recoverImportBatchAfterTimeout();
      if (!recovered) showError(contextualErrorMessage('Import failed', error?.message));
    }
  } finally {
    if (spin) spin.destroy();
    if (importBtn) { importBtn.disabled = false; importBtn.innerHTML = original; }
    const input = document.getElementById('memory-import-file');
    if (input) input.value = '';
  }
}

async function handleImportFile(file) {
  return handleImportFiles([file]);
}

async function handleImportFileLegacy(file) {
  if (!file) return;

  const sessionId = sessionModule?.getCurrentSessionId?.();

  const importBtn = document.getElementById('memory-import-btn');
  const _origImportHtml = importBtn ? importBtn.innerHTML : '';
  let importSpin = null;
  if (importBtn) {
    importBtn.disabled = true;
    importBtn.innerHTML = '';
    importSpin = spinnerModule.createWhirlpool(12);
    importSpin.element.style.cssText = 'width:12px;height:12px;margin:0 5px 0 0;display:inline-flex;vertical-align:-2px;transform:translateY(-1px);';
    importBtn.appendChild(importSpin.element);
    importBtn.appendChild(document.createTextNode('Importing'));
  }

  try {
    const formData = new FormData();
    formData.append('file', file);
    if (sessionId) {
        formData.append('session', sessionId);
    }

    const res = await fetch(`${window.location.origin}/api/memory/import`, {
      method: 'POST',
      body: formData
    });

    if (!res.ok) {
      const failure = await responseError(res, 'Import failed');
      const error = new Error(failure.message);
      error.problem = failure.problem;
      throw error;
    }

    const data = await res.json();
    const suggestions = data.suggestions || [];
    pendingMemoryImportFile = null;

    // Show suggestions using the existing suggestions UI
    const modal = document.getElementById('memory-modal');
    const body = document.getElementById('memory-suggestions-body');
    if (!body) return;

    body.innerHTML = '';
    body.classList.remove('hidden');

    const memList = document.getElementById('memory-list');
    if (memList) memList.classList.add('hidden');

    if (suggestions.length === 0) {
      body.innerHTML = '<div class="memory-empty">No useful information found in file.</div>';
    } else {
      const reviewItems = suggestions
        .map((s) => ({
          text: typeof s === 'string' ? s : s.text,
          category: (typeof s === 'object' && s.category) || 'fact',
          active: true,
        }))
        .filter((s) => s.text);
      const header = document.createElement('div');
      header.className = 'memory-suggestions-header';
      const headerTitle = document.createElement('span');
      const updateHeaderTitle = () => {
        const remaining = reviewItems.filter((item) => item.active).length;
        headerTitle.textContent = `Imported from ${data.filename || file.name} (${remaining}) Review`;
      };
      updateHeaderTitle();
      const headerActions = document.createElement('div');
      headerActions.className = 'memory-suggestions-actions';
      const backBtn = document.createElement('button');
      backBtn.className = 'memory-item-btn';
      backBtn.textContent = 'back';
      backBtn.addEventListener('click', () => {
        body.classList.add('hidden');
        body.innerHTML = '';
        if (memList) memList.classList.remove('hidden');
      });
      const saveAllBtn = document.createElement('button');
      saveAllBtn.className = 'memory-item-btn save';
      saveAllBtn.textContent = 'save all';
      saveAllBtn.addEventListener('click', async () => {
        let saved = 0;
        let pending = 0;
        for (const s of reviewItems) {
          if (!s.active || !s.text) continue;
          try {
            const response = await fetch(`${window.location.origin}/api/memory/add`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ text: s.text, category: s.category })
            });
            const result = await response.json();
            if (!response.ok) continue;
            if (result.pending_review) pending++;
            saved++;
          } catch (e) { /* skip */ }
        }
        body.classList.add('hidden');
        body.innerHTML = '';
        if (memList) memList.classList.remove('hidden');
        await loadMemories();
        document.querySelector('.memory-tab[data-memory-tab="browse"]')?.click();
        showToast(
          pending
            ? `Sent ${pending} ${pending === 1 ? 'memory' : 'memories'} to review`
            : `Saved ${saved} memories`
        );
      });
      headerActions.appendChild(saveAllBtn);
      headerActions.appendChild(backBtn);
      header.appendChild(headerTitle);
      header.appendChild(headerActions);
      body.appendChild(header);

      reviewItems.forEach(item => {
        const div = document.createElement('div');
        div.className = 'memory-suggestion-item';

        const content = document.createElement('div');
        content.className = 'memory-item-content';
        const txt = document.createElement('textarea');
        txt.className = 'memory-item-edit-input';
        txt.rows = 2;
        txt.value = item.text;
        txt.setAttribute('aria-label', 'Imported memory text');
        txt.addEventListener('input', () => {
          item.text = txt.value.trim();
          btn.disabled = !item.text;
        });
        const catSelect = document.createElement('select');
        catSelect.className = 'memory-edit-cat-select';
        catSelect.setAttribute('aria-label', 'Imported memory category');
        for (const category of MEMORY_CATEGORIES) {
          const option = document.createElement('option');
          option.value = category;
          option.textContent = categoryLabel(category);
          option.selected = category === item.category;
          catSelect.append(option);
        }
        catSelect.addEventListener('change', () => { item.category = catSelect.value; });
        content.appendChild(txt);
        content.appendChild(catSelect);

        const actionWrap = document.createElement('div');
        actionWrap.className = 'memory-suggestion-actions';
        const btn = document.createElement('button');
        btn.className = 'memory-item-btn save';
        btn.textContent = 'save';
        btn.addEventListener('click', async () => {
          try {
            const response = await fetch(`${window.location.origin}/api/memory/add`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ text: item.text, category: item.category })
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || 'Failed to save memory');
            item.active = false;
            div.remove();
            updateHeaderTitle();
            btn.disabled = true;
            btn.textContent = result.pending_review ? 'in review' : 'saved';
            // Keep the Browse tab backed by the committed provider result.
            // The save-all path already refetches; single-item saves must do
            // the same or the new memory appears saved but remains absent
            // until a later full reload.
            await loadMemories();
            showToast(result.pending_review ? 'Sent to Candidates for review' : 'Saved to memory');
          } catch (error) {
            console.error('Failed to save imported memory:', error);
            showError(error.message || 'Failed to save memory');
          }
        });
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'memory-item-btn delete';
        deleteBtn.textContent = 'delete';
        deleteBtn.addEventListener('click', () => {
          item.active = false;
          div.remove();
          updateHeaderTitle();
        });
        actionWrap.appendChild(btn);
        actionWrap.appendChild(deleteBtn);

        div.appendChild(content);
        div.appendChild(actionWrap);
        body.appendChild(div);
      });
    }

    modal.classList.remove('hidden');
    document.querySelector('.memory-tab[data-memory-tab="browse"]')?.click();
  } catch (error) {
    console.error('Import failed:', error);
    if (error?.problem?.code === 'MEMORY_ROUTE_UNCONFIGURED') {
      _showMemoryRouteProblem(error.problem, file, error.message);
    } else {
      showError(contextualErrorMessage('Import failed', error?.message));
    }
  } finally {
    if (importSpin) importSpin.destroy();
    if (importBtn) {
      importBtn.disabled = false;
      importBtn.innerHTML = _origImportHtml;
    }
    // Reset file input so the same file can be re-selected
    const fileInput = document.getElementById('memory-import-file');
    if (fileInput) fileInput.value = '';
  }
}

// Utility aliases (canonical implementations live in uiModule)
var showToast = uiModule.showToast;
var showError = uiModule.showError;

// Event listeners
document.addEventListener('DOMContentLoaded', () => {
  _wireMemoryDrag();
  _wireMemoryNukeControls();
  _wireHandlerProfileControls();

  // Memory modal tabs
  document.querySelectorAll('.memory-tab[data-memory-tab]').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.memoryTab;
      document.querySelectorAll('.memory-tab').forEach(t => t.classList.toggle('active', t === tab));
      document.querySelectorAll('.memory-tab-panel[data-memory-panel]').forEach(p => {
        p.classList.toggle('hidden', p.dataset.memoryPanel !== target);
      });
      // Lazy-load skills tab (cascade=true → play the domino-in entrance)
      if (target === 'skills') {
        import('./skills.js').then(m => { if (m.loadSkills) m.loadSkills(true); else if (m.default?.loadSkills) m.default.loadSkills(true); });
      }
      if (target === 'inspect') loadMemoryInspect();
      if (target === 'graph') {
        const tagFilter = graphState.nextTagFilter;
        graphState.nextTagFilter = null;
        loadMemoryGraph({ tagFilter });
      }
      if (target === 'digest') loadDigestPreview();
    });
  });

  const sortSelect = document.getElementById('memory-sort');
  if (sortSelect) {
    sortSelect.addEventListener('change', () => {
      sortOrder = sortSelect.value;
      renderMemoryList();
    });
  }
  _initMemorySortPicker();

  const tidyBtn = document.getElementById('memory-tidy-btn');
  if (tidyBtn) tidyBtn.addEventListener('click', tidyMemories);

  const selectBtn = document.getElementById('memory-select-btn');
  if (selectBtn) selectBtn.addEventListener('click', () => {
    if (selectMode) exitSelectMode();
    else enterSelectMode();
  });

  const selectAll = document.getElementById('memory-select-all');
  if (selectAll) selectAll.addEventListener('change', toggleSelectAll);

  const bulkBar = document.getElementById('memory-bulk-bar');
  if (bulkBar) bulkBar.addEventListener('click', (e) => {
    if (e.target.closest('button') || e.target === selectAll) return;
    selectAll.checked = !selectAll.checked;
    selectAll.dispatchEvent(new Event('change'));
  });

  const bulkDeleteBtn = document.getElementById('memory-bulk-delete');
  if (bulkDeleteBtn) bulkDeleteBtn.addEventListener('click', bulkDelete);

  const bulkCancelBtn = document.getElementById('memory-bulk-cancel');
  if (bulkCancelBtn) bulkCancelBtn.addEventListener('click', exitSelectMode);

  const exportBtn = document.getElementById('memory-export-btn');
  if (exportBtn) exportBtn.addEventListener('click', exportMemories);

  const importBtn = document.getElementById('memory-import-btn');
  if (importBtn) importBtn.addEventListener('click', importMemories);

  const importFile = document.getElementById('memory-import-file');
  if (importFile) importFile.addEventListener('change', (e) => {
    const selected = Array.from(e.target.files || []);
    if (selected.length) handleImportFiles(selected);
  });

  window.addEventListener('memory-refresh', () => {
    loadMemories();
  });
  const inspectTier = document.getElementById('memory-inspect-tier');
  const inspectStatus = document.getElementById('memory-inspect-status');
  const inspectRefresh = document.getElementById('memory-inspect-refresh');
  if (inspectTier) inspectTier.addEventListener('change', loadMemoryInspect);
  if (inspectStatus) inspectStatus.addEventListener('change', loadMemoryInspect);
  if (inspectRefresh) inspectRefresh.addEventListener('click', loadMemoryInspect);
});

document.addEventListener('open-clank:providers-updated', () => {
  const files = pendingMemoryImportFiles.length
    ? [...pendingMemoryImportFiles]
    : (pendingMemoryImportFile ? [pendingMemoryImportFile] : []);
  if (!files.length || memoryImportRetrying) return;
  memoryImportRetrying = true;
  setTimeout(() => {
    handleImportFiles(files).finally(() => { memoryImportRetrying = false; });
  }, 0);
});

const memoryModule = {
  loadMemories,
  renderMemoryList,
  updateMemoryCount,
  addNewMemory,
  editMemory,
  deleteMemory,
  extractMemory,
  buildCategoryChips,
  tidyMemories,
  importMemories,
  exportMemories,
  loadMemoryInspect,
};

export default memoryModule;
window.memoryModule = memoryModule;
