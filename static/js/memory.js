import { bindCardActionMenu } from './cardActionMenu.js';
import { recordPresentation } from './achievementProducer.js';
import { uiIcon } from './uiIcons.js';
import { withRequirement, blocksPublicLinks } from './standingGuidanceRequirement.js';
// Memory Management Functions
// This module handles all memory-related operations

import uiModule from './ui.js';
import sessionModule from './sessions.js';
import spinnerModule from './spinner.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';
import { memoryChips, isTrusted, DEFAULT_KIND_TRUST } from './util/memoryTrust.js';
import { forceLayout, hitTest, tagCounts, mergeExpansion } from './util/memoryGraph.js';
import { contextualErrorMessage, responseError } from './util/httpError.js';
import { memoryAssetUrl } from './util/memoryExport.js';
import { createMemoryExportDialog, downloadMemoryAsset } from './memoryExportDialog.js';
import { dismissAllPendingImportBatches } from './memoryImportDismiss.js';
import Storage, { KEYS } from './storage.js';
import { mountMemoryBatchReview } from './memoryBatchReview.js';
import { memoryRelationReview } from './memoryRelationReview.js';
import { openMemoryUpdateBatch } from './memoryUpdateBatch.js';
import { mountJournalCoverage } from './memoryJournalCoverage.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { mountShadowRanking } from './memoryShadowRanking.js';
import { openKnowledgeSnapshotExport, openKnowledgeSnapshotImport, openKnowledgeSnapshotBundleExport, openKnowledgeSnapshotBundleImport } from './knowledgeSnapshot.js';

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
let pairedInspect = null;
let manualAddInspect = null;
let preextractedInspect = [];
let memoryInspectGeneration = 0;
let disposeMemoryBatchReview = null;

function _pairedScope() {
  const scope = window.chatModule?.ratingAccountScope?.();
  return scope && scope.owner && document.body?.dataset?.accountId === scope.owner
    ? {owner:scope.owner, authEpoch:scope.authEpoch} : null;
}
function _pairedCurrent(scope) {
  const now = _pairedScope();
  return !!scope && !!now && now.owner === scope.owner && now.authEpoch === scope.authEpoch;
}
function _mergePairedRow(previous, item) {
  const original=item.original || previous?.original || null;
  const retained=previous?._candidateReviewPending;
  const value={...(previous || {}),...item,original};
  if (retained) value._candidateReviewPending=retained;
  if (retained || (original && item.status === 'pending') || item.status === 'unknown') value.status='unknown';
  if (item.content === null && (original || retained || item.status === 'unknown')) value.content=null;
  return value;
}
function _pairedIntent(value) {
  return !!value && ['accept','reject'].includes(value.action) && typeof value.reason === 'string'
    && typeof value.request_key === 'string' && !!value.request_key
    && /^[a-f0-9]{64}$/.test(value.candidate_digest || '') && /^[a-f0-9]{64}$/.test(value.association_sha256 || '');
}

let pendingMemoryImportFile = null;
let pendingMemoryImportFiles = [];
let pendingMemoryImportIntent = null;
let memoryImportRetrying = false;
// Per-user trust prefs (memory_trust_auto + memory_trust_auto_kinds),
// loaded with the list so the trusted/reference chip reflects reality.
let trustPrefsState = {};
// Signal filters (kind / provenance / trust) — fm-provider only.
let signalFilters = { kind: 'all', provenance: 'all', trust: 'all' };

async function _loadTrustPrefs(isCurrent = () => true) {
  try {
    const response = await fetch('/api/prefs', { credentials: 'same-origin' });
    if (response.ok) { const prefs = await response.json(); if (isCurrent()) trustPrefsState = prefs || {}; }
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

async function syncDerivationCapabilities() {
  const toggle = document.getElementById('auto-skills-toggle');
  const card = toggle?.closest('.admin-card');
  if (!card) return;
  let status = card.querySelector('[data-derivation-status]');
  if (!status) {
    status = document.createElement('span');
    status.dataset.derivationStatus = 'true';
    status.className = 'admin-toggle-sub';
    status.style.display = 'block';
    card.appendChild(status);
  }
  status.textContent = 'Automatic skill processing status is unavailable.';
  try {
    const response = await fetch('/api/memory?limit=1', { credentials: 'same-origin' });
    if (!response.ok) return;
    const data = await response.json();
    if (data?.derivation_capabilities?.automatic_skills?.state === 'unavailable') {
      status.textContent = 'Automatic skill extraction is awaiting source-qualified processing, even when enabled. Native automatic memory also awaits admission and writer authority.';
    }
  } catch (_) { /* Leave the honest unavailable status. */ }
}

let s02PolicyStatusGeneration = 0;
async function syncS02PolicyAvailability() {
  const generation=++s02PolicyStatusGeneration, scope=_pairedScope();
  const axes=[['importance','importance'],['connections','connections'],['consolidation','merge']];
  const nodes=axes.map(([name])=>document.getElementById(`memory-policy-${name}-status`));
  if (!scope || nodes.some(node=>!node)) return;
  for (const node of nodes) node.textContent='Checking profile availability…';
  try {
    const [policyResponse,predicateResponse]=await Promise.all([
      fetch('/api/prefs/memory-policy/preview',{credentials:'same-origin',cache:'no-store'}),
      fetch('/api/memory/policy/connection-predicates',{credentials:'same-origin',cache:'no-store'}).catch(()=>null)
    ]);
    if (!policyResponse.ok) throw new Error('unavailable');
    const policy=await policyResponse.json();
    const predicate=predicateResponse?.ok ? await predicateResponse.json().catch(()=>null) : null;
    if (generation!==s02PolicyStatusGeneration || !_pairedCurrent(scope) || nodes.some(node=>!node.isConnected)) return;
    for (const [index,[name,axis]] of axes.entries()) {
      const toggle=document.getElementById(`memory-policy-${name}-toggle`);
      const modes=policy?.capabilities?.axes?.[axis];
      const available=Array.isArray(modes) && modes.length>0;
      const choice=toggle?.dataset.prefLoaded==='1' ? (toggle.checked ? 'Saved choice: on.' : 'Saved choice: off.') : 'Saved choice unavailable.';
      let detail=available ? 'Profile support is available; execution still depends on current authority and eligible sources.' : 'Profile support is unavailable.';
      if (name==='connections' && available) {
        const selected=predicate?.selected;
        const valid=predicate?.version===1 && selected && Array.isArray(predicate.predicates)
          && predicate.predicates.some(item=>JSON.stringify(item)===JSON.stringify(selected));
        if (!valid) detail='Profile support is available; a current registered predicate must be selected.';
      }
      nodes[index].textContent=`${choice} ${detail}`;
    }
  } catch (_) {
    if (generation===s02PolicyStatusGeneration && _pairedCurrent(scope))
      for (const node of nodes) if (node.isConnected) node.textContent='Profile availability unavailable; saved choice is retained.';
  }
}

function retireS02PolicyAvailability() {
  s02PolicyStatusGeneration++;
  for (const name of ['importance','connections','consolidation']) {
    const node=document.getElementById(`memory-policy-${name}-status`);
    if (node) node.textContent='Profile availability unavailable.';
  }
}
for (const type of ['openclank:auth-context-changed','workspace-change','pagehide']) {
  window.addEventListener(type, retireS02PolicyAvailability);
  if (type==='openclank:auth-context-changed') document.addEventListener(type, retireS02PolicyAvailability);
}

async function syncConnectionPredicate() {
  const select=document.getElementById('memory-connection-predicate');
  const status=document.getElementById('memory-connection-predicate-status');
  const scope=_pairedScope(); if (!select || !status || !scope) return;
  select.disabled=true; status.textContent='Loading predicate options…';
  const load=async () => {
    const response=await fetch('/api/memory/policy/connection-predicates',{credentials:'same-origin'});
    if (!response.ok) throw new Error('unavailable');
    const data=await response.json();
    if (!_pairedCurrent(scope) || !select.isConnected || data?.version!==1 || !Array.isArray(data.predicates)) throw new Error('changed');
    const values=[null,...data.predicates]; select.replaceChildren();
    for (const [index,item] of values.entries()) {
      const option=document.createElement('option'); option.value=String(index);
      option.textContent=item===null?'Not selected':`${item.predicate} (version ${item.version})`; select.appendChild(option);
    }
    const index=values.findIndex(item=>JSON.stringify(item)===JSON.stringify(data.selected ?? null));
    select.value=String(index<0?0:index);select.disabled=false;
    status.textContent=index<0?'Saved predicate is unavailable or changed; select a current option.':'Choose an explicitly registered predicate.';
    return values;
  };
  try {
    let values=await load();
    select.onchange=async () => {
      if (!_pairedCurrent(scope)) return;
      const selected=values[Number(select.value)];if (selected===undefined) return;
      select.disabled=true;
      try {
        const response=await fetch('/api/memory/policy/connection-predicates',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected})});
        if (!response.ok || !_pairedCurrent(scope)) throw new Error('unavailable');
        values=await load();
        void syncS02PolicyAvailability();
      } catch (_) {if (_pairedCurrent(scope)) {status.textContent='Predicate could not be saved; refresh and try again.';select.disabled=true;}}
    };
  } catch (_) {if (_pairedCurrent(scope)) {status.textContent='Predicate options unavailable.';select.disabled=true;}}
}


async function initMemoryRankLearning() {
  const root=document.getElementById('memory-rank-learning'), save=document.getElementById('memory-rank-save');
  const status=document.getElementById('memory-rank-status');
  if (!root || !save || !status || root.dataset.bound) return;
  root.dataset.bound='1'; let snapshot=null, scope=null, generation=0, busy=false;
  const names=['enabled','salience','age_decay','retrieval_boost'];
  const controls=Object.fromEntries(names.map(name=>[name,root.querySelector(`[data-rank="${name}"]`)]));
  async function load() {
    const epoch=++generation; scope=_pairedScope(); snapshot=null; root.disabled=true;
    if (!scope) {status.textContent='Ranking settings are unavailable.';return;}
    try {
      const response=await fetch('/api/prefs/memory-policy/preview',{credentials:'same-origin',cache:'no-store'});
      const value=await response.json();
      if (epoch!==generation || !_pairedCurrent(scope)) return;
      if (!response.ok || value.owner!==scope.owner || !Number.isInteger(value.revision)) throw new Error();
      const doc=value.document, accepted=['openclank.memory-policy.v2','openclank.memory-policy.v3'].includes(doc?.schema) ? doc.accepted_policy : doc;
      const policy=accepted?.policy || value.proposed_defaults;
      if (!policy || typeof policy!=='object') throw new Error();
      const rank=policy.rank_learning || {enabled:false,salience:false,age_decay:false,retrieval_boost:false};
      if (names.some(name=>typeof rank[name]!=='boolean')) throw new Error();
      for (const name of names) controls[name].checked=rank[name];
      snapshot={owner:value.owner,revision:value.revision,policy:structuredClone(policy)};
      const ready=value.capabilities?.consumers?.rank_learning===true;
      root.disabled=!ready && !rank.enabled;
      for (const name of names.slice(1)) controls[name].disabled=!ready;
      status.textContent=ready ? 'Preferences are available. Signal effects depend on the current evidence profile and saved weights.' : 'Ranking is unavailable for this memory profile; saved choices are retained. You can turn it off.';
    } catch (_) {if (epoch===generation && _pairedCurrent(scope)) status.textContent='Ranking settings are unavailable.';}
  }
  save.addEventListener('click',async()=>{
    if (busy || !snapshot || !_pairedCurrent(scope)) return;
    const prior=snapshot, ticket=scope, epoch=generation;
    const policy=structuredClone(prior.policy);
    policy.rank_learning=Object.fromEntries(names.map(name=>[name,controls[name].checked]));
    busy=true;root.disabled=true;status.textContent='Saving ranking preferences…';
    try {
      const check=await fetch('/api/prefs/memory-policy/preview',{credentials:'same-origin',cache:'no-store'});
      const fresh=await check.json();
      if (epoch!==generation || !_pairedCurrent(ticket)) return;
      if (!check.ok || fresh.owner!==prior.owner || fresh.revision!==prior.revision || (fresh.capabilities?.consumers?.rank_learning!==true && policy.rank_learning.enabled)) throw new Error();
      const response=await fetch('/api/prefs/memory-policy',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:prior.revision,policy})});
      if (epoch!==generation || !_pairedCurrent(ticket)) return;
      if (!response.ok) throw new Error();
      document.dispatchEvent(new CustomEvent('openclank:memory-policy-changed'));
      await load();
      if (_pairedCurrent(ticket)) status.textContent='Ranking preferences saved. Signal effects depend on available evidence and saved weights.';
    } catch (_) {if (epoch===generation && _pairedCurrent(ticket)) {status.textContent='Settings changed or could not be saved. Refresh and try again.';snapshot=null;}}
    finally {busy=false;}
  });
  root._refreshRankLearning=load;
  document.addEventListener('openclank:auth-context-changed',()=>{generation++;snapshot=null;root.disabled=true;void load();});
  document.addEventListener('odysseus:session-selected',()=>{void load();});
  await load();
}

function _retainedRunTokenSummary(tokens) {
  if (!tokens) return '';
  const counts = value => `${value.input} input, ${value.output} output`;
  const reported = tokens.supplierReported;
  return ` Reserved tokens ${counts(tokens.reserved)}. Charged tokens ${counts(tokens.charged)}. `
    + (reported ? `Supplier-reported tokens ${counts(reported)}.` : 'Supplier-reported tokens unavailable.');
}

async function loadRetainedConsolidationRun() {
  const label = document.getElementById('memory-retained-run');
  const scope = _pairedScope();
  if (!label || !scope) return;
  let journal = document.getElementById('memory-journal-retained-run');
  if (!journal) {
    journal = document.createElement('div');
    journal.id = 'memory-journal-retained-run';
    journal.className = 'admin-toggle-sub';
    label.after(journal);
  }
  label.textContent = 'Last retained verified summary run: unavailable.';
  journal.textContent = 'Last retained verified Journal model run: unavailable.';
  for (const [target, kind, name] of [[label, 'summary', 'summary'], [journal, 'journal_synthesis', 'Journal model']]) {
    if (!_pairedCurrent(scope) || !target.isConnected) return;
    try {
      const response = await fetch(`/api/memory/consolidation-retained-run?output_kind=${kind}`,
        { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (!_pairedCurrent(scope) || !target.isConnected) return;
      if (!response.ok || data?.coverage !== 'retained_history') continue;
      if (data.state === 'empty') {
        target.textContent = `No verified ${name} run was found in retained history.`;
        continue;
      }
      const run = data.lastVerifiedRetainedRun;
      const when = new Date(run?.settledAtMs);
      if (data.state === 'available' && Number.isFinite(when.getTime())
          && ['staged', 'no_proposal', 'failed', 'cancelled'].includes(run?.outcome)) {
        target.textContent = `Last retained verified ${name} run: ${when.toLocaleString()} (${run.outcome.replace('_', ' ')}).`
          + _retainedRunTokenSummary(run.tokens);
      }
    } catch (_) { /* Keep the unavailable status for this output kind. */ }
  }
}

let retainedConnectionGeneration=0;
async function loadRetainedConnectionRun() {
  const label=document.getElementById('memory-connection-retained-run'), scope=_pairedScope();
  const generation=++retainedConnectionGeneration;
  if (!label || !scope) return;
  label.textContent='Last retained verified connection model run: unavailable.';
  try {
    const response=await fetch('/api/memory/consolidation-retained-run?output_kind=connection',{credentials:'same-origin',cache:'no-store'});
    const data=await response.json();
    if (generation!==retainedConnectionGeneration || !_pairedCurrent(scope) || !label.isConnected || !response.ok || data?.coverage!=='retained_history') return;
    if (data.state==='empty') {label.textContent='No verified connection model run was found in retained history.';return;}
    if (data.state!=='available') return;
    const run=data.lastVerifiedRetainedRun, when=new Date(run?.settledAtMs);
    if (!Number.isFinite(when.getTime()) || !['staged','no_proposal','failed','cancelled'].includes(run?.outcome)) return;
    label.textContent=`Last retained verified connection model run: ${when.toLocaleString()} (${run.outcome.replace('_',' ')}).`
      + _retainedRunTokenSummary(run.tokens);
  } catch (_) {}
}
function retireRetainedConnectionRun() {
  retainedConnectionGeneration++;
  const label=document.getElementById('memory-connection-retained-run');
  if (label) label.textContent='Last retained verified connection model run: unavailable.';
}
window.addEventListener('pagehide',retireRetainedConnectionRun);
window.addEventListener('workspace-change',retireRetainedConnectionRun);
window.addEventListener('openclank:auth-context-changed',retireRetainedConnectionRun);
document.addEventListener('openclank:auth-context-changed',retireRetainedConnectionRun);

async function syncJournalInterval() {
  const input=document.getElementById('memory-journal-interval');
  const button=document.getElementById('memory-journal-interval-save');
  const status=document.getElementById('memory-journal-interval-status');
  if (!input || !button || !status) return;
  const scope=_pairedScope(); input.disabled=true;button.disabled=true;input.value='';
  status.textContent='Interval unavailable.';
  if (!scope) return;
  const load=async () => {
    const response=await fetch('/api/prefs/memory_policy_consolidation_interval_days',{credentials:'same-origin',cache:'no-store'});
    const data=await response.json();
    if (!_pairedCurrent(scope) || !response.ok || data.account_id!==scope.owner || !(data.value===null || (Number.isInteger(data.value) && data.value>=1 && data.value<=365))) throw new Error('unavailable');
    input.value=data.value===null ? '' : String(data.value); input.disabled=false;button.disabled=false;
    status.textContent='Sets how often Memery automatically consolidates chat records.';
  };
  button.onclick=async () => {
    if (!_pairedCurrent(scope)) {input.disabled=true;button.disabled=true;input.value='';return;}
    const value=input.value.trim()==='' ? null : Number(input.value);
    if (value!==null && (!Number.isInteger(value) || value<1 || value>365)) {status.textContent='Enter 1–365 days or leave blank.';return;}
    input.disabled=true;button.disabled=true;
    try {
      const response=await fetch('/api/prefs/memory_policy_consolidation_interval_days',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({value,expected_account_id:scope.owner})});
      if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
      await load(); if (_pairedCurrent(scope)) status.textContent='Chat memory consolidation interval saved.';
    } catch (_) {if (_pairedCurrent(scope)) status.textContent='Interval could not be saved. Reopen Memory to retry.';}
  };
  try {await load();} catch (_) {if (_pairedCurrent(scope)) status.textContent='Interval unavailable.';}
}

async function syncDocumentVectorProfile() {
  const box=document.getElementById('memory-document-vector');
  const select=document.getElementById('memory-document-vector-family');
  const save=document.getElementById('memory-document-vector-save');
  const build=document.getElementById('memory-document-vector-build');
  const status=document.getElementById('memory-document-vector-status');
  if (!box || !select || !save || !build || !status) return;
  const scope=_pairedScope();box.disabled=true;
  if (!scope) {status.textContent='Profile unavailable.';return;}
  const presets={exact:{version:1,family:'exact'},ivf_minibatch:{version:1,family:'ivf_minibatch',seed:19,lists:2,iterations:2,effort:2,visit_budget:512},ivf_flat:{version:1,family:'ivf_flat',seed:19,lists:2,iterations:4,effort:2,visit_budget:512},hnsw:{version:1,family:'hnsw',seed:19,m:4,ef_construction:16,effort:16,visit_budget:512},scalar8:{version:1,family:'scalar8',transform:{kind:'identity'},candidate_limit:64},pq:{version:1,family:'pq',seed:9,transform:{kind:'identity'},parts:2,centroids:2,iterations:4,candidate_limit:64}};
  let requested=null,supported=false;
  async function load() {
    const response=await fetch('/api/prefs/document-vector-profile',{credentials:'same-origin'});
    if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
    const data=await response.json();
    if (!_pairedCurrent(scope) || data.version!==1 || !data.requested || !presets[data.requested.family] || typeof data.profile_supported!=='boolean') throw new Error('unavailable');
    requested=data.requested;supported=data.profile_supported;select.value=requested.family;box.disabled=false;
    build.disabled=!supported || requested.family==='exact';
    status.textContent=requested.family==='exact' ? 'Exact search selected.' : supported ? 'Preference saved. Build explicitly; search uses exact fallback until a compatible profile is available.' : 'Profile support is unavailable; search uses exact fallback.';
  }
  select.onchange=() => {build.disabled=!supported || !requested || select.value!==requested.family || requested.family==='exact';};
  save.onclick=async () => {
    if (!_pairedCurrent(scope) || !requested || !presets[select.value]) return;
    const desired=select.value===requested.family ? requested : presets[select.value];box.disabled=true;
    try {
      const response=await fetch('/api/prefs/document-vector-profile',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected:requested,selection:desired})});
      if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
      await load();
    } catch (_) {if (_pairedCurrent(scope)) status.textContent='Preference could not be saved. Reopen Memory to retry.';}
  };
  build.onclick=async () => {
    if (!_pairedCurrent(scope) || !requested || !supported || requested.family==='exact') return;
    box.disabled=true;status.textContent='Building the selected profile…';
    try {
      const response=await fetch('/api/personal/vector-profile/build',{method:'POST',credentials:'same-origin'});
      if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
      const result=await response.json();if (result.state!=='available' || result.family!==requested.family) throw new Error('unavailable');
      await load();
      if (_pairedCurrent(scope)) {
        status.textContent = 'Profile built for the current document generation. Later changes may require another explicit build.';
        const sizes = result.payload_bytes;
        if (sizes && ['original_vectors', 'derived_profile', 'profile_descriptor']
            .every((key) => Number.isSafeInteger(sizes[key]) && sizes[key] > 0)) {
          const bytes = (value) => `${value.toLocaleString()} bytes`;
          status.textContent += ` Stored payloads: original vectors ${bytes(sizes.original_vectors)}, derived profile ${bytes(sizes.derived_profile)}, descriptor ${bytes(sizes.profile_descriptor)}.`;
        }
      }
    } catch (_) {if (_pairedCurrent(scope)) {status.textContent='Profile build unavailable or outcome unconfirmed. Exact search remains available.';box.disabled=false;}}
  };
  try {await load();} catch (_) {if (_pairedCurrent(scope)) status.textContent='Profile unavailable.';}
}

async function syncDocumentVectorBuild() {
  const box=document.getElementById('memory-document-build');
  const build=document.getElementById('memory-document-build-start');
  const check=document.getElementById('memory-document-build-check');
  const resume=document.getElementById('memory-document-build-resume');
  const cancel=document.getElementById('memory-document-build-cancel');
  const status=document.getElementById('memory-document-build-status');
  if (!box || !build || !check || !resume || !cancel || !status) return;
  const scope=_pairedScope();box.disabled=true;
  if (!scope) {status.textContent='Document embedding build unavailable.';return;}
  let key=null,operation=null,storageReady=false,rowCount=0,routeReady=false;
  const idValid=value=>typeof value==='string' && /^[A-Za-z0-9_-]{16,128}$/.test(value);
  const setButtons=()=>{
    box.disabled=false;
    build.disabled=!storageReady || !routeReady || !!operation || rowCount===0;
    check.disabled=cancel.disabled=!storageReady || !operation;
    resume.disabled=!storageReady || !routeReady || !operation;
  };
  const call=async (action,body)=>{
    const response=await fetch('/api/personal/vector-build/'+action,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
    const result=await response.json();if (!_pairedCurrent(scope) || !result || typeof result.state!=='string') throw new Error('unavailable');
    return result;
  };
  const report=result=>{
    if (result.state==='published') {
      const direct=result.version===1 && result.current===true && typeof result.generation_id==='string' && /^gen_vec_[0-9a-f]{64}$/.test(result.generation_id);
      const retained=(result.version===1 || result.version===2) && result.operation_id===operation && result.outcome && result.outcome.state==='published';
      if (!direct && !retained) throw new Error('unavailable');
      localStorage.removeItem(key);operation=null;status.textContent='Document embeddings are ready.';
    } else if (result.state==='terminal') {
      if ((result.version!==1 && result.version!==2) || result.operation_id!==operation || !['published','cancelled'].includes(result.terminal)) throw new Error('unavailable');
      localStorage.removeItem(key);operation=null;
      status.textContent=result.terminal==='published' ? 'The previous build finished. Current documents may need a new build.' : 'The previous build was canceled.';
    } else if (result.state==='cancelled') {
      if ((result.version!==1 && result.version!==2) || result.operation_id!==operation || result.paid_outcome!=='unknown') throw new Error('unavailable');
      localStorage.removeItem(key);operation=null;status.textContent='Build canceled. An earlier request may still have completed.';
    } else if (result.operation_id!==operation || (result.version!==1 && result.version!==2)) throw new Error('unavailable');
    else if (result.state==='ready_to_continue') status.textContent='A batch was saved. Continue to process the remaining documents.';
    else if (result.state==='queued') status.textContent='Build prepared. Continue when ready.';
    else if (result.state==='probe_complete') status.textContent='The model check completed. Continue to recover or process the next batch.';
    else if (result.state==='unknown') status.textContent='The outcome is not confirmed. Check or continue; requests are not automatically repeated.';
    else status.textContent='Build outcome unavailable. Keep this build to check again or cancel.';
  };
  try {
    const response=await fetch('/api/personal/vector-build/capability',{credentials:'same-origin'});
    if (!_pairedCurrent(scope) || !response.ok) throw new Error('unavailable');
    const info=await response.json();
    if (!_pairedCurrent(scope) || info.version!==1 || info.state!=='ready' || info.account_id!==scope.owner
        || typeof info.workspace_id!=='string' || !info.workspace_id || info.profile!=='document_vector_batch.v2'
        || typeof info.route_ready!=='boolean' || !Number.isInteger(info.row_count) || info.row_count<0 || info.row_count>100000) throw new Error('unavailable');
    rowCount=info.row_count;routeReady=info.route_ready;
    key='openclank.document-vector.v2:'+encodeURIComponent(scope.owner)+':'+encodeURIComponent(info.workspace_id)+':'+info.profile;
    operation=localStorage.getItem(key);storageReady=operation===null || idValid(operation);
    if (!storageReady) {status.textContent='Saved build ID is unavailable.';return;}
    status.textContent=operation ? (routeReady ? 'A saved build can be checked or continued.' : 'The saved build can be checked or canceled; the configured model route is unavailable.') : !rowCount ? 'No indexed documents to build.' : routeReady ? 'Ready to build embeddings for indexed documents.' : 'The configured model route is unavailable.';
    setButtons();
  } catch (_) {if (_pairedCurrent(scope)) status.textContent='Document embedding build unavailable.';return;}
  build.onclick=async ()=>{
    if (!_pairedCurrent(scope) || !storageReady || !routeReady || operation || !rowCount) return;
    box.disabled=true;status.textContent='Preparing the document build…';
    try {
      const result=await call('prepare',{});
      if (result.version!==1 || result.state!=='queued' || !idValid(result.operation_id)) throw new Error('unavailable');
      operation=result.operation_id;
      try {
        localStorage.setItem(key,operation);
        if (localStorage.getItem(key)!==operation) throw new Error('storage');
      } catch (_) {
        operation=null;storageReady=false;status.textContent='Could not save the build ID; no embedding request was sent.';
        if (_pairedCurrent(scope)) setButtons();return;
      }
      status.textContent='Building document embeddings…';
      report(await call('execute',{operation_id:operation}));
    } catch (_) {if (_pairedCurrent(scope)) status.textContent=operation ? 'Build outcome unconfirmed. Check, continue, or cancel this build.' : 'Build preparation unavailable.';}
    if (_pairedCurrent(scope)) setButtons();
  };
  check.onclick=async ()=>{
    if (!_pairedCurrent(scope) || !operation) return;box.disabled=true;
    try {report(await call('status',{operation_id:operation}));}
    catch (_) {if (_pairedCurrent(scope)) status.textContent='Build status unavailable. The saved build has not been discarded.';}
    if (_pairedCurrent(scope)) setButtons();
  };
  resume.onclick=async ()=>{
    if (!_pairedCurrent(scope) || !routeReady || !operation) return;box.disabled=true;status.textContent='Checking and continuing the saved build…';
    try {report(await call('resume',{operation_id:operation}));}
    catch (_) {if (_pairedCurrent(scope)) status.textContent='Build outcome unconfirmed. Check again or cancel this build.';}
    if (_pairedCurrent(scope)) setButtons();
  };
  cancel.onclick=async ()=>{
    if (!_pairedCurrent(scope) || !operation) return;box.disabled=true;
    try {report(await call('cancel',{operation_id:operation}));}
    catch (_) {if (_pairedCurrent(scope)) status.textContent='Cancellation unconfirmed. The saved build has not been discarded.';}
    if (_pairedCurrent(scope)) setButtons();
  };
}

let learningObserver;
function mountLearningSettings(host) {
  const controls = document.getElementById('memory-learning-controls');
  const home = document.querySelector('[data-memory-learning-home]');
  const panel = host?.closest('[data-settings-panel]');
  const modal = host?.closest('#settings-modal');
  if (!controls || !home || !panel || !modal) return;
  learningObserver?.disconnect();
  host.append(controls);
  learningObserver = new MutationObserver(() => {
    if (modal.classList.contains('hidden') || panel.classList.contains('hidden')) {
      home.append(controls);
      learningObserver.disconnect();
    }
  });
  for (const node of [panel, modal]) learningObserver.observe(node, { attributes: true, attributeFilter: ['class'] });
  void syncLearningControls();
}

async function syncLearningControls() {
  const toggles = Promise.all([
    syncPrefToggle('memory-assistant-interpretation-toggle', 'memory_assistant_interpretation', 'Assistant interpretation review enabled', 'Assistant interpretation review disabled', false),
    syncPrefToggle('memory-policy-importance-toggle', 'memory_policy_importance_enabled', 'Importance preference saved as on', 'Importance preference saved as off', false),
    syncPrefToggle('memory-policy-connections-toggle', 'memory_policy_connections_enabled', 'Connections preference saved as on', 'Connections preference saved as off', false),
    syncPrefToggle('memory-policy-consolidation-toggle', 'memory_policy_consolidation_enabled', 'Consolidation preference saved as on', 'Consolidation preference saved as off', false),
  ]);
  void syncConnectionPredicate();
  void loadRetainedConsolidationRun();
  void loadRetainedConnectionRun();
  void syncJournalInterval();
  await toggles;
  void syncS02PolicyAvailability();
}

async function syncToggles() {
  // Settings and the workspace header use the same account preference.
  await syncPrefToggle('memory-enabled-header-toggle', 'memory_enabled', 'Memory enabled', 'Memory disabled', false);
  // The Skills header toggle owns the `skills_enabled` pref (was never wired —
  // toggling it did nothing, so skills stayed on). Now it actually gates skill
  // injection (see chat_helpers.py: uprefs.skills_enabled).
  await syncPrefToggle('skills-enabled-header-toggle', 'skills_enabled', 'Skills enabled', 'Skills disabled', false);
  await syncMemoryMode();
  await syncLearningControls();
  void syncDocumentVectorProfile();
  void syncDocumentVectorBuild();
  void document.getElementById('memory-rank-learning')?._refreshRankLearning?.();
  await syncPrefToggle('memory-shadow-ranking-toggle', 'memory_shadow_ranking_enabled', 'Shadow ranking enabled', 'Shadow ranking disabled', false);
  await syncPrefToggle('memory-trust-auto-toggle', 'memory_trust_auto', 'Auto-captured memories can be trusted (per kind below)', 'Auto-captured memories stay behind the firewall', false);
  await _wireTrustKindSwitches();
  await syncPrefToggle('auto-skills-toggle', 'auto_skills', 'Auto-extract skills enabled', 'Auto-extract skills disabled', false);
  void syncDerivationCapabilities();
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
  // Qualified Memory prepares these inquiries without a model call. They remain
  // unsurfaced until the user chooses otherwise.
  const message = 'Profile question preparation is unavailable until Memory identity, policy, and its guarded writer are qualified. Questions are not asked automatically.';
  _setProfileQuestionStatus(message);
  showToast(message);
}

let _seedPreferenceGeneration = 0;
async function _refreshQuestionSeedPreference() {
  const seeds = document.getElementById('memory-question-seeds');
  if (!seeds || seeds.dataset.saving === '1') return;
  const generation = ++_seedPreferenceGeneration;
  const ticket = _memoryActionTicket();
  seeds.disabled = true;
  seeds.checked = false;
  try {
    const response = await fetch('/api/prefs/memory_question_seeds', { credentials:'same-origin' });
    if (!response.ok) throw new Error('Question setting unavailable');
    const data = await response.json();
    if (!_memoryActionCurrent(ticket) || generation !== _seedPreferenceGeneration) return;
    if (typeof data.value !== 'boolean') throw new Error('Question setting unavailable');
    seeds.checked = data.value;
    seeds.disabled = false;
  } catch (_) {
    if (_memoryActionCurrent(ticket) && generation === _seedPreferenceGeneration)
      _setProfileQuestionStatus('Question setting unavailable.', 'error');
  }
}

function _wireHandlerProfileControls() {
  document.getElementById('memory-add-profile-questions')?.addEventListener('click', _addHandlerProfileQuestions);
  const seeds = document.getElementById('memory-question-seeds');
  if (!seeds) return;
  _refreshQuestionSeedPreference();
  seeds.addEventListener('change', async () => {
    ++_seedPreferenceGeneration;
    const changeTicket = _memoryActionTicket();
    const wanted = seeds.checked;
    seeds.dataset.saving = '1';
    seeds.disabled = true;
    try {
      const response = await fetch('/api/prefs/memory_question_seeds', { method:'PUT', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:wanted}) });
      if (!_memoryActionCurrent(changeTicket)) return;
      if (!response.ok) throw new Error('Question setting could not be saved');
      const data = await response.json();
      if (!_memoryActionCurrent(changeTicket)) return;
      if (data.value !== wanted) throw new Error('Question setting could not be saved');
    } catch (error) {
      if (_memoryActionCurrent(changeTicket)) { seeds.checked = !wanted; _setProfileQuestionStatus(error.message, 'error'); }
    } finally {
      delete seeds.dataset.saving;
      if (_memoryActionCurrent(changeTicket)) seeds.disabled = false;
    }
  });
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
    nukeButton.disabled = memoryNukeBusy;
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
  if (detail?.count_unavailable === true) return null;
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

function _memoryNukeSelective(detail, completed = false) {
  if (!detail || typeof detail !== 'object' || !Object.hasOwn(detail, 'selective')) return null;
  const value = detail.selective, count = n => Number.isSafeInteger(n) && n >= 0;
  const keys = ['version','state','unit','erasable_candidates','retained_approved_candidates','retained_raw','retained_recovery','retained_quarantine','retained_jobs','partition_sha256'];
  const unitCount = (item, unit) => item && typeof item === 'object' && !Array.isArray(item)
    && Object.keys(item).length === 2 && item.unit === unit && count(item.count);
  const valid = value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
    && value.version === 1 && value.state === 'complete' && value.unit === 'candidate'
    && count(value.erasable_candidates) && count(value.retained_approved_candidates)
    && unitCount(value.retained_raw, 'raw_record') && unitCount(value.retained_recovery, 'recovery_unit')
    && unitCount(value.retained_quarantine, 'quarantine_record') && unitCount(value.retained_jobs, 'job_record')
    && /^[a-f0-9]{64}$/.test(value.partition_sha256 || '');
  if (!valid) return 'Candidate classification unavailable.';
  return `Unreviewed or rejected candidates ${completed ? 'removed' : 'selected for removal'}: ${value.erasable_candidates}; approved candidates retained as memory dependencies: ${value.retained_approved_candidates}. Retained raw records: ${value.retained_raw.count}; recovery units: ${value.retained_recovery.count}; quarantine records: ${value.retained_quarantine.count}; whole job payloads: ${value.retained_jobs.count}. Retained raw records, quarantine records, recovery units, and job payloads may contain unreviewed text.`;
}

function _memoryNukeConfirmationMessage(preview, requested) {
  const expanded = _memoryNukeExpandedComponents(preview.expanded_components);
  const components = preview.components && typeof preview.components === 'object'
    ? preview.components
    : {};
  const included = [...new Set((expanded.length ? expanded : requested).filter(Boolean))];
  const selective=Object.hasOwn(components.ingest || {}, 'selective');
  const lines = ['The following account data is selected for reset:'];
  for (const component of included) {
    const count = _memoryNukeCount(components[component]);
    lines.push(`• ${_memoryNukeLabel(component)}${count === null ? '' : selective && component === 'ingest' ? ` — ${count} inventoried database ${count === 1 ? 'record' : 'records'}` : ` — ${count} inventoried ${count === 1 ? 'entry' : 'entries'}`}`);
    if (component === 'ingest') { const detail=_memoryNukeSelective(components[component]); if (detail) lines.push(`  ${detail}`); }
  }
  const added = included.filter(component => !requested.includes(component));
  if (added.length) {
    lines.push('', `Required additions: ${added.map(_memoryNukeLabel).join(', ')}`);
  }
  const implications = _memoryNukeImplications(preview.implications).filter(item => !selective || item !== 'Selected live memory data is permanently cleared for this account.');
  if (implications.length) {
    lines.push('', 'What this means:');
    for (const implication of implications.slice(0, 6)) lines.push(`• ${implication}`);
    if (implications.length > 6) lines.push(`• ${implications.length - 6} more implications`);
  }
  lines.push('', selective
    ? 'Selected live ingest will be removed. Old restore operations that would revive erased ingest are blocked. Existing exports, backups, and source files remain. Other users and built-in or shared skills are not touched.'
    : 'Selected live data will be removed. Content-free original, reset, and replay-denial facts remain. Existing exports, backups, and source files remain. Other users and built-in or shared skills are not touched.');
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
  const hasSelective = Object.hasOwn(result.categories?.ingest || {}, 'selective');
  for (const component of Object.keys(MEMORY_NUKE_COMPONENTS)) {
    const detail = result.categories?.[component];
    if (!detail) continue;
    const count = _memoryNukeCount(detail);
    const state = _boundedMemoryNukeText(detail.state, 60);
    const error = _boundedMemoryNukeText(detail.error, 180);
    const known = detail.count_unavailable === true && Number.isFinite(Number(detail.count))
      ? ` (known ${Math.max(0, Number(detail.count))}; total unavailable)` : '';
    const selective=_memoryNukeSelective(detail, result.complete === true && detail.state === 'complete');
    parts.push(`${_memoryNukeLabel(component)}${count === null ? known : selective && component === 'ingest' ? ` ${count} inventoried database ${count === 1 ? 'record' : 'records'}` : ` ${count} inventoried ${count === 1 ? 'entry' : 'entries'}`}${state ? ` (${state})` : ''}${error ? ` — ${error}` : ''}${selective ? ` · ${selective}` : ''}`);
  }
  const serverMessage = _boundedMemoryNukeText(result.message, 400);
  const retained = hasSelective ? '' : ' Content-free original, reset, and replay-denial facts remain.';
  if (serverMessage && parts.length) return `${serverMessage} ${parts.join(' · ')}${retained}`;
  if (serverMessage) return `${serverMessage}${retained}`;
  if (parts.length) return `${parts.join(' · ')}${retained}`;
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
  memoryNukeBusy = true;
  _setMemoryNukeStatus('Counting the selected data…');
  _updateMemoryNukeControls();
  try {
    const recovery = await _postMemoryNuke({ action: 'recovery_preview' }, 'Could not check Brain reset recovery');
    if (recovery.status === 'none' && !components.length) {
      _setMemoryNukeStatus('Select at least one Brain data category.');
      return;
    }
    const preview = recovery.status === 'none'
      ? await _postMemoryNuke({ action: 'preview', components }, 'Could not preview Brain reset')
      : recovery;
    _validateMemoryNukePreview(preview);
    const selected = preview.recovery === true ? preview.requested_components : components;
    if (!Array.isArray(selected) || !selected.length) throw new Error('Brain reset recovery selection is unavailable');
    const confirmed = await uiModule.styledConfirm(
      _memoryNukeConfirmationMessage(preview, selected),
      {
        title: preview.recovery === true ? 'Resume Brain reset?' : 'Nuke selected Brain data?',
        confirmText: preview.recovery === true ? 'Resume reset' : 'Nuke selected data',
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

document.addEventListener('openclank:memery-preferences-saved', () => {
  void syncPrefToggle('memory-enabled-header-toggle', 'memory_enabled', 'Memery enabled', 'Memery disabled', false);
  void syncMemoryMode();
});

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
        const legacy = await fetch('/api/prefs/auto_memory');
        const legacyData = legacy.ok ? await legacy.json() : {};
        select.value = typeof legacyData.value === 'boolean' ? (legacyData.value ? 'automatic' : 'off') : 'manual';
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
  const scoped = Boolean(toggle.closest('#memory-learning-controls'));
  const scope = scoped && _pairedScope();
  const ticket = {};
  toggle._prefTicket = ticket;
  const current = () => toggle._prefTicket === ticket && (!scoped || (scope && _pairedCurrent(scope)));
  if (scoped) {
    toggle._prefCurrent = current;
    toggle.disabled = true;
    toggle.checked = false;
  }
  delete toggle.dataset.prefLoaded;
  try {
    const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`);
    if (res.ok) {
      const data = await res.json();
      if (!current()) return;
      toggle.checked = data.value !== false;
      if (scoped) toggle.disabled = false;
      toggle.dataset.prefLoaded = '1';
    }
  } catch (e) {
    console.error(`Failed to load ${prefKey} pref:`, e);
  }
  if (dimBelow) syncToggleDim(toggle);
  if (!toggle.dataset.bound) {
    toggle.dataset.bound = '1';
    toggle.addEventListener('change', async () => {
      const isCurrent = scoped ? toggle._prefCurrent : () => true;
      if (!isCurrent()) return;
      if (dimBelow) syncToggleDim(toggle);
      try {
        const res = await fetch(`${window.location.origin}/api/prefs/${prefKey}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: toggle.checked })
        });
        if (!isCurrent()) return;
        if (!res.ok) {
          console.error(`PUT ${prefKey} returned ${res.status}`);
          toggle.checked = !toggle.checked; // revert
          if (dimBelow) syncToggleDim(toggle);
          showError('Failed to save preference');
          return;
        }
        toggle.dataset.prefLoaded = '1';
        showToast(toggle.checked ? onMsg : offMsg);
        if (['memory_policy_importance_enabled','memory_policy_connections_enabled','memory_policy_consolidation_enabled'].includes(prefKey)) void syncS02PolicyAvailability();
      } catch (e) {
        if (!isCurrent()) return;
        console.error(`Failed to save ${prefKey} pref:`, e);
        toggle.checked = !toggle.checked; // revert
        if (dimBelow) syncToggleDim(toggle);
        showError('Failed to save preference');
      }
    });
  }
}

async function fetchMemoryPages(isCurrent = () => true) {
  const memory = [];
  let cursor = null;
  let provider = 'native';
  do {
    if (!isCurrent()) return null;
    const params = new URLSearchParams({ limit: '1000' });
    if (cursor) params.set('cursor', cursor);
    const response = await fetch(`${window.location.origin}/api/memory?${params}`);
    if (!response.ok) {
      const error = new Error(`Memory provider unavailable (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    if (!isCurrent()) return null;
    const page = Array.isArray(data) ? data : (data.memory || []);
    memory.push(...page);
    provider = data?.provider || provider;
    cursor = data?.next_cursor || null;
  } while (cursor);
  return { memory, provider };
}

export async function loadMemories({ isCurrent = null } = {}) {
  const current = isCurrent || (() => true);
  if (!current()) return;
  if (!isCurrent) {
    _refreshQuestionSeedPreference();
    _ensureNewMemoryCategorySelect();
    memoriesLoading = true;
    renderMemoryList();
    updateMemoryCount();
  }
  try {
    const [data] = await Promise.all([fetchMemoryPages(current), _loadTrustPrefs(current)]);
    if (!current()) return;
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
    if (!current()) return;
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
  if (!isCurrent) syncToggles();
  // Surface durable import batches whose review was stranded (for example by
  // a proxy read timeout on the long-running import POST).
  if (!isCurrent) _refreshPendingImportNotice();
}

function _systemSeedInquiry(item) {
  const rationale = item.current?.activation_rationale;
  const provenance = rationale?.question_context?.provenance;
  if (!provenance || provenance.version !== 1 || provenance.kind !== 'system_seed' ||
      !['handler.preferred_address', 'assistant.operating_boundary'].includes(provenance.seed_key)) return null;
  return typeof rationale.question_text === 'string' && rationale.question_text.length <= 512
    ? rationale.question_text : null;
}

// A supplied fact staged by Native: provenance is set there, never by the caller. Shown as source reference + "inferred".
function _preextractedSource(item) {
  const source = item?.provenance?.kind === 'preextracted' ? item.provenance.source
    : (item?.evidence_role === 'inferred' && item.source && typeof item.source === 'object' ? item.source : null);
  if (source && typeof source.uri === 'string' && Number.isSafeInteger(source.revision)) return source;
  if (source?.kind === 'host_user_body_anchor' && source.coverage === 'host_user_body_anchor_only'
      && Object.keys(source).sort().join(',') === 'coverage,kind,navigation,reference'
      && ['unverified', 'stale', 'unavailable'].includes(source.navigation)
      && source.reference && Number.isSafeInteger(source.reference.revision) && source.reference.revision > 0) {
    // A persisted review snapshot cannot establish present Host navigation.
    return item?.provenance?.kind === 'preextracted' ? {...source, navigation: 'unverified'} : source;
  }
  return null;
}

function _preextractedSourceLabel(source) {
  if (source?.kind === 'host_user_body_anchor') {
    const nav = ['stale', 'unavailable'].includes(source.navigation) ? source.navigation : 'unverified';
    return `Host user-body snapshot (revision ${source.reference.revision}; navigation ${nav})`;
  }
  return source ? `${source.uri} (revision ${source.revision})` : 'source unavailable';
}

function _preextractedCard(entry) {
  const card = document.createElement('div'); card.className = 'memory-item'; card.dataset.preextracted = entry.candidate_id;
  const body = document.createElement('div'); body.className = 'memory-item-content';
  const text = document.createElement('div'); text.className = 'memory-item-text'; text.textContent = entry.text || '';
  const source = _preextractedSource(entry);
  const line = document.createElement('div'); line.className = 'memory-inferred-line';
  const tag = document.createElement('span'); tag.className = 'memory-inferred-badge'; tag.textContent = 'inferred';
  line.append(tag, document.createTextNode(` ${entry.kind || 'fact'} · Source: ${_preextractedSourceLabel(source)}`));
  body.append(text, line);
  if (entry.conflict) { const c = document.createElement('div'); c.className = 'admin-toggle-sub'; c.textContent = 'Conflicts with a protected memory field; nothing was overwritten.'; body.append(c); }
  const actions = document.createElement('div'); actions.style.cssText = 'display:flex;gap:6px;margin-top:8px';
  for (const [label, action] of [['Approve', 'accept'], ['Reject', 'reject']]) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn' + (action === 'reject' ? ' danger' : ''); button.textContent = label;
    button.addEventListener('click', () => reviewPreextractedCandidate(entry, action, button)); actions.append(button);
  }
  body.append(actions); card.append(body); return card;
}

async function reviewPreextractedCandidate(entry, action, button) {
  const ticket = _memoryActionTicket(); button.disabled = true;
  const post = async (path, payload) => {
    const response = await fetch(`/api/memory/candidates/preextracted/${path}`, { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error('This fact could not be reviewed. It may have changed; reload and try again.');
    return data;
  };
  try {
    const preview = await post('review-preview', { candidate_id: entry.candidate_id });
    if (!_memoryActionCurrent(ticket) || preview.state !== 'preview' || preview.text !== entry.text) throw new Error('The fact changed while opening review. Reload and try again.');
    const source = _preextractedSource(preview) || _preextractedSource(entry);
    const ok = await uiModule.styledConfirm(`${action === 'accept' ? 'Add to your memories' : 'Reject'}?\n\n${preview.text}\n\nInferred from ${_preextractedSourceLabel(source)}.`, { title: 'Review inferred fact', confirmText: action === 'accept' ? 'Approve' : 'Reject', danger: action === 'reject' });
    if (!ok || !_memoryActionCurrent(ticket)) return;
    await post('review', { candidate_id: entry.candidate_id, candidate_digest: preview.candidate_digest, action, request_key: crypto.randomUUID(), reason: 'owner_review' });
    showToast(action === 'accept' ? 'Fact added to your memories' : 'Fact rejected');
    await Promise.all([loadMemoryInspect(), loadMemories()]);
  } catch (error) { showError(error.message || 'Review unavailable'); }
  finally { button.disabled = false; }
}

// A journal-synthesis proposal is stored as its proposal object; the review list shows the proposed text and the cited parts, never raw JSON.
function _journalProposalText(content) {
  try {
    const proposal = JSON.parse(content);
    if (proposal?.version !== 's08.policy-journal.v1' || typeof proposal.content !== 'string' || !Array.isArray(proposal.journal?.rows)) return null;
    const cited = proposal.journal.rows.map((row) => `message ${row.message_id} · part ${row.part_id} · revision ${row.revision}`).join('; ');
    return `${proposal.content}\n\nJournal synthesis from chat ${proposal.journal.chat_id}. Cited: ${cited || 'none'}. Inferred, not yet approved.`;
  } catch (_) { return null; }
}

function _journalSourceControl(card, item) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'memory-toolbar-btn';
  button.textContent = 'Review Journal sources';
  const details = document.createElement('div');
  details.className = 'memory-details-drawer';

  async function preview(current) {
    const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review-preview`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'accept', reason: 'approved_by_user' }),
    });
    const result = await response.json();
    const journal = result.proposal?.journal;
    if (!current() || !response.ok || result.state !== 'preview' || result.candidate_id !== item.id
        || result.output_kind !== 'journal_synthesis' || !/^[a-f0-9]{64}$/.test(result.candidate_digest || '')
        || typeof journal?.chat_id !== 'string' || !journal.chat_id || !Array.isArray(journal.cited)
        || !journal.cited.length || journal.cited.length > 128
        || journal.cited.some((row) => typeof row?.message_id !== 'string' || typeof row.part_id !== 'string'
          || !Number.isSafeInteger(row.revision) || row.revision < 1)) {
      throw new Error('The original Journal sources are unavailable or have changed.');
    }
    return { digest: result.candidate_digest, journal };
  }

  button.addEventListener('click', async () => {
    const ticket = _memoryActionTicket();
    const current = () => _memoryActionCurrent(ticket) && card.isConnected;
    button.disabled = true;
    try {
      const original = await preview(current);
      const list = document.createElement('ul');
      for (const row of original.journal.cited) {
        const citation = document.createElement('li');
        citation.textContent = `Message ${row.message_id} · part ${row.part_id} · revision ${row.revision}`;
        list.appendChild(citation);
      }
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'memory-toolbar-btn';
      open.textContent = 'Open source chat';
      open.addEventListener('click', async () => {
        if (!current()) return;
        open.disabled = true;
        try {
          const latest = await preview(current);
          if (latest.digest !== original.digest || JSON.stringify(latest.journal) !== JSON.stringify(original.journal)) {
            throw new Error('The original Journal sources changed. Review them again.');
          }
          document.getElementById('memory-modal')?.classList.add('hidden');
          await sessionModule.selectSession(latest.journal.chat_id);
        } catch (error) {
          if (current()) showError(error.message || 'The source chat is unavailable.');
        } finally { open.disabled = false; }
      });
      details.replaceChildren(list, open);
    } catch (error) {
      if (current()) details.textContent = error.message || 'The Journal sources are unavailable.';
    } finally { button.disabled = false; }
  });
  card.append(button, details);
}


function _inspectText(item, tier) {
  if (tier === 'candidate') return _journalProposalText(item.content) || item.content || (['assistant_interpretation','explicit_add_original'].includes(item.review_kind) ? 'Original review status only; proposal unavailable.' : item.review_kind === 'explicit_add_terminal' ? 'Original review was erased.' : '');
  if (tier === 'quarantine') return item.content || '';
  if (tier === 'history') return (item.current?.status === 'open' && _systemSeedInquiry(item)) || item.current?.text || item.latest?.text || '';
  return item.content || item.text || '';
}

function _inspectMeta(item, tier) {
  if (tier === 'candidate' && item.review_kind === 'assistant_interpretation') return `Assistant interpretation · ${item.status || 'unknown'}`;
  const meta = item.metadata || item.payload || {};
  const provenance = meta.provenance || {};
  const sessionId = item.session_id || provenance.session_id;
  const parts = [
    tier === 'history' && _systemSeedInquiry(item) ? 'System-generated inquiry' : null,
    tier === 'candidate' && item.review_kind === 'assistant_interpretation' ? 'Assistant interpretation' : null,
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
  const ticket = _memoryActionTicket();
  if (!_memoryActionCurrent(ticket)) return { cancelled: true };
  const legacyId = item.id || item.block_id;
  const revision = item.current_revision || item.latest_revision;
  let id = legacyId;
  let body = new URLSearchParams({ expected_revision: String(revision),
    ...Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, String(value)])) });
  const questionReview = ['question_defer', 'question_dismiss', 'question_reopen'].includes(action);
  if ((['retract', 'revert'].includes(action) || questionReview) && item.block_id) {
    let pending = item._pendingPublishedReview;
    if (pending && (pending.action !== action || pending.revision !== revision || pending.target !== (fields.target_revision ?? null))) {
      throw new Error('A prior reviewed request may have completed. Retry it or reload before reviewing another action.');
    }
    if (!pending) {
      const request = { action, expected_revision: revision, target_revision: fields.target_revision ?? null, request_key: crypto.randomUUID() };
      const response = await fetch(`/api/memory/${encodeURIComponent(item.block_id)}/published-preview`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
      });
      if (!_memoryActionCurrent(ticket)) return { cancelled: true };
      const preview = await response.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket)) return { cancelled: true };
      if (!response.ok) throw new Error(_memoryError(preview, 'Memory review is unavailable'));
      if (preview.mode === 'guarded') {
        if (preview.block_id !== item.block_id || preview.action !== action || preview.expected_revision !== revision ||
            preview.target_revision !== request.target_revision || preview.request_key !== request.request_key ||
            !Array.isArray(preview.selected_paths) || !preview.selected_paths.length || !Array.isArray(preview.changes) ||
            typeof preview.operation_json !== 'string' || !/^[0-9a-f]{64}$/.test(preview.operation_sha256 || '')) {
          throw new Error('Memory review is unavailable');
        }
        const summary = action === 'retract' ? 'Retract this memory?' : questionReview
          ? ({question_defer:'Defer this question?', question_dismiss:'Dismiss this question?', question_reopen:'Make this question eligible again?'})[action]
          : `Restore revision ${request.target_revision}?\n\n${preview.changes.map(change => `${change.path}: ${JSON.stringify(change.before?.value)} → ${JSON.stringify(change.after?.value)}`).join('\n')}`;
        if (!await uiModule.styledConfirm(summary, { title: 'Review memory change', confirmText: 'Apply' })) return { cancelled: true };
        if (!_memoryActionCurrent(ticket)) return { cancelled: true };
        pending = { action, revision, target: request.target_revision, preview };
        item._pendingPublishedReview = pending;
      } else if (preview.mode !== 'legacy' || questionReview) throw new Error('Memory review is unavailable');
    }
    if (pending) {
      id = item.block_id;
      body = new URLSearchParams({ expected_revision: String(pending.revision), request_key: pending.preview.request_key,
        selected_paths: JSON.stringify(pending.preview.selected_paths), operation_json: pending.preview.operation_json,
        operation_sha256: pending.preview.operation_sha256 });
      if (pending.target !== null) body.set('target_revision', String(pending.target));
      if (questionReview) body.set('action', action);
    }
  }
  if (!_memoryActionCurrent(ticket)) return { cancelled: true };
  const endpoint = questionReview ? 'question-review' : action;
  const response = await fetch(`/api/memory/${encodeURIComponent(id)}/${endpoint}`, { method: 'POST', credentials: 'same-origin', body });
  if (!_memoryActionCurrent(ticket)) return { cancelled: true };
  const data = await response.json().catch(() => ({}));
  if (!_memoryActionCurrent(ticket)) return { cancelled: true };
  if (!response.ok) throw new Error(_memoryError(data, `Memory ${action} failed`));
  delete item._pendingPublishedReview;
  await Promise.all([loadMemoryInspect({ isCurrent: () => _memoryActionCurrent(ticket) }), loadMemories({ isCurrent: () => _memoryActionCurrent(ticket) })]);
  if (!_memoryActionCurrent(ticket)) return { cancelled: true };
  return data;
}

function _publishedActionToast(result, fallback) {
  if (result?.cancelled) return;
  const receipt = result?.review;
  if (receipt && receipt.current_revision !== receipt.applied_revision) {
    showToast(`Applied revision ${receipt.applied_revision}; current revision ${receipt.current_revision}`);
  } else showToast(fallback);
}

const _explicitAddEdits = new Set();
const _ownedReviewCandidate = id => /^(?:manualadd|importreview)_[a-f0-9]{64}$/.test(id || '');
async function _editInspectedCandidate(card, item) {
  const editKey = `${document.body?.dataset?.accountId || ''}:${item.id}`;
  if (_ownedReviewCandidate(item.id) && (_explicitAddReviews.has(editKey) || _explicitAddEdits.has(editKey))) {
    showError('The original candidate action is unresolved.'); return;
  }
  let captured = null;
  if (_ownedReviewCandidate(item.id)) {
    const ticket = _memoryActionTicket(), account = document.body?.dataset?.accountId;
    try {
      const originalResponse = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phase:'original'})});
      const original = await originalResponse.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account || !card.isConnected) return;
      if (!originalResponse.ok || original.state !== 'absent') throw new Error('Check the original review status before editing.');
      const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review-preview`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'accept'}) });
      const preview = await response.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account || !card.isConnected) return;
      if (!response.ok || preview.review_kind !== 'explicit_add' || preview.candidate_id !== item.id
          || !Number.isSafeInteger(preview.expected_revision) || !/^[a-f0-9]{64}$/.test(preview.binding_sha256 || '')
          || !preview.proposal || typeof preview.proposal.text !== 'string' || !MEMORY_CATEGORIES.includes(preview.proposal.category))
        throw new Error('Candidate preview unavailable.');
      captured = preview;
    } catch (error) { if (_memoryActionCurrent(ticket)) showError(error.message || 'Candidate preview unavailable.'); return; }
  }
  const editor = document.createElement('div');
  editor.className = 'memory-inline-editor';
  const text = document.createElement('textarea');
  text.className = 'memory-item-edit-input';
  text.rows = 3;
  // The candidate list renders identity tokens for display; the editor must
  // round-trip the stored (raw) form.
  text.value = captured ? captured.proposal.text : (item.raw_content || item.content || '');
  text.setAttribute('aria-label', 'Candidate text');
  const category = document.createElement('select');
  category.className = 'memory-edit-cat-select';
  category.setAttribute('aria-label', 'Candidate category');
  for (const name of MEMORY_CATEGORIES) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = categoryLabel(name);
    option.selected = name === (captured ? captured.proposal.category : (item.kind || item.category));
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
      let body = { text: content, category: category.value };
      if (captured) {
        body = {expected_revision:captured.expected_revision, expected_binding:captured.binding_sha256,
                text:content, category:category.value, source:captured.proposal.source, question_context:captured.proposal.question_context};
      }
      if (_ownedReviewCandidate(item.id)) _explicitAddEdits.add(editKey);
      const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}`, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(_memoryError(data, 'Candidate edit failed'));
      _explicitAddEdits.delete(editKey);
      showToast('Candidate updated');
      await loadMemoryInspect();
    } catch (error) {
      showError(_explicitAddEdits.has(editKey) ? 'Candidate edit outcome is unknown; refresh before further action.' : (error.message || 'Candidate edit failed'));
      save.disabled = _explicitAddEdits.has(editKey);
    }
  });
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:6px;margin-top:8px';
  actions.append(save, cancel);
  editor.append(text, category, actions);
  card.replaceChildren(editor);
}

async function reviewPairedCandidate(item, action, button) {
  const scope=_pairedScope(); if (!scope) { showError('The original account is unavailable.'); return; }
  const workspace=Storage.get(KEYS.WORKSPACE_ID,'') || '', page=pairedInspect;
  const filter=document.getElementById('memory-inspect-status')?.value || '';
  const current=()=>_pairedCurrent(scope) && pairedInspect === page && (Storage.get(KEYS.WORKSPACE_ID,'') || '') === workspace
    && document.getElementById('memory-inspect-tier')?.value === 'candidate'
    && (document.getElementById('memory-inspect-status')?.value || '') === filter;
  button.disabled=true;
  try {
    let intent=item._candidateReviewPending || item.original || null;
    if (intent && !_pairedIntent(intent)) throw new Error('Original review status is unavailable.');
    if (action !== 'status' && intent) throw new Error('Check the original review status first.');
    if (action === 'status' && !intent) throw new Error('Original review status is unavailable.');
    if (!intent) {
      const reason=action === 'accept' ? 'approved_by_user' : 'rejected_by_user';
      const response=await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review-preview`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action,reason})});
      const preview=await response.json().catch(()=>({}));
      if (!current()) return;
      if (!response.ok || preview.mode !== 'guarded' || preview.state !== 'preview'
          || preview.review_kind !== 'assistant_interpretation' || preview.candidate_id !== item.id
          || typeof preview.proposal !== 'string' || !/^[a-f0-9]{64}$/.test(preview.candidate_digest || '')
          || !/^[a-f0-9]{64}$/.test(preview.association_sha256 || '')) throw new Error('Assistant interpretation review is unavailable.');
      if (preview.proposal !== item.content) throw new Error('The proposal changed. Refresh before review.');
      if (!current()) return;
      intent={action,reason,candidate_digest:preview.candidate_digest,association_sha256:preview.association_sha256,request_key:crypto.randomUUID()};
      item._candidateReviewPending=intent;
    }
    const response=await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
      method:'POST', credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phase:action === 'status' ? 'status' : 'apply',accept:intent.action === 'accept',
        workspace_id:item.workspace_id,candidate_digest:intent.candidate_digest,association_sha256:intent.association_sha256,
        request_key:intent.request_key,reason:intent.reason})});
    const result=await response.json().catch(()=>({}));
    if (!current()) return;
    if (!response.ok || result.candidate_id !== item.id || !['applied','rejected','unknown'].includes(result.state))
      throw new Error('Review outcome is unknown. Check the original status.');
    if (result.state === 'unknown') throw new Error('Review outcome is unknown. Check the original status.');
    delete item._candidateReviewPending;
    showToast(result.visibility === 'not_rechecked' ? 'Review settled; current visibility has not been checked.' : 'Assistant interpretation review settled.');
    const reloadCurrent=()=>_pairedCurrent(scope) && (Storage.get(KEYS.WORKSPACE_ID,'') || '') === workspace
      && document.getElementById('memory-inspect-tier')?.value === 'candidate'
      && (document.getElementById('memory-inspect-status')?.value || '') === filter;
    await Promise.all([loadMemoryInspect({isCurrent:reloadCurrent}),loadMemories({isCurrent:reloadCurrent})]);
  } catch (error) { if (current()) showError(error.message || 'Review status unavailable.'); }
  finally { if (current()) { button.disabled=false; renderMemoryInspect(); } }
}

const _explicitAddReviews = new Map();
async function reviewInspectedCandidate(item, accept, button) {
  if (_ownedReviewCandidate(item.id)) return reviewExplicitAddCandidate(item, accept, button);
  button.disabled = true;
  try {
    const reason = accept ? 'approved_by_user' : 'rejected_by_user';
    let pending = item._candidateReviewPending;
    if (pending && pending.accept !== accept) throw new Error('Retry the pending review before choosing another action.');
    if (!pending) {
      const previewResponse = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review-preview`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: accept ? 'accept' : 'reject', reason }),
      });
      const preview = await previewResponse.json().catch(() => ({}));
      if (!previewResponse.ok) throw new Error(preview.detail || 'Candidate preview unavailable');
      if (preview.mode === 'legacy') {
        pending = { accept, reason, workspace_id: item.workspace_id };
      } else if (preview.mode === 'guarded' && preview.review_kind === 's08_policy' && preview.state === 'original') {
        const original=preview.original, receipt=preview.receipt;
        if (!['connection','summary','journal_synthesis'].includes(preview.output_kind)) throw new Error('Original S08 review unavailable.');
        if (!original || original.candidate_id!==item.id || original.candidate_revision!==3
            || !/^[0-9a-f]{64}$/.test(original.candidate_sha256 || '') || typeof original.request_key!=='string'
            || typeof original.reason!=='string' || !['accept','reject'].includes(original.action) || !receipt || !['applied','rejected','unknown'].includes(receipt.state))
          throw new Error('Original connection review unavailable.');
        if (receipt.state==='applied' || receipt.state==='rejected') {
          showToast('Review was previously settled; current visibility has not been checked.');
          await Promise.all([loadMemoryInspect(),loadMemories()]); return;
        }
        pending={accept:original.action==='accept',action:original.action,reason:original.reason,workspace_id:item.workspace_id,candidate_digest:original.candidate_sha256,
          expected_v2_head:3,selected_paths:original.action==='accept'?['/value']:[],request_key:original.request_key,review_kind:'s08_policy',output_kind:preview.output_kind,phase:'status'};
      } else if (preview.mode === 'guarded') {
        if (!/^[0-9a-f]{64}$/.test(preview.candidate_digest || '')) throw new Error('Candidate preview unavailable');
        pending = { accept, reason, workspace_id: item.workspace_id, candidate_digest: preview.candidate_digest,
          request_key: crypto.randomUUID() };
        if (preview.review_kind === 's08_policy') {
          if (!['connection','summary','journal_synthesis'].includes(preview.output_kind)) throw new Error('S08 review unavailable.');
          pending.review_kind='s08_policy'; pending.output_kind=preview.output_kind; pending.action=accept?'accept':'reject'; pending.phase='apply';
        }
        if (accept) {
          if (!Number.isSafeInteger(preview.expected_v2_head) || preview.expected_v2_head < 0 || JSON.stringify(preview.selected_paths) !== '["/value"]') throw new Error('Candidate preview unavailable');
          pending.expected_v2_head = preview.expected_v2_head;
          pending.selected_paths = preview.selected_paths;
        } else if (preview.review_kind === 's08_policy') {
          if (preview.expected_v2_head!==3 || JSON.stringify(preview.selected_paths)!=='[]') throw new Error('Candidate preview unavailable');
          pending.expected_v2_head=3; pending.selected_paths=[];
        } else {
          if (!/^[0-9a-f]{64}$/.test(preview.association_sha256 || '')) throw new Error('Candidate preview unavailable');
          if (typeof preview.source_chat_id !== 'string' || !preview.source_chat_id) throw new Error('Candidate preview unavailable');
          pending.association_sha256 = preview.association_sha256;
          pending.source_chat_id = preview.source_chat_id;
        }
      } else throw new Error('Candidate review unavailable');
      item._candidateReviewPending = pending;
    }
    const outgoing={...pending};
    if (pending.review_kind==='s08_policy') pending.phase='status';
    const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(outgoing),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Review failed; check the original status.');
    if (pending.review_kind==='s08_policy' && data.candidate_id===item.id && data.state==='terminal_erased') {
      delete item._candidateReviewPending;
      showToast('The candidate was erased; review cannot continue.');
      await Promise.all([loadMemoryInspect(),loadMemories()]); return;
    }
    if (pending.review_kind==='s08_policy' && (data.candidate_id!==item.id || data.state!==(pending.accept?'applied':'rejected')))
      throw new Error('Review outcome is unknown. Check the original status.');
    delete item._candidateReviewPending;
    showToast(data.visibility === 'not_rechecked'
      ? 'Review was previously applied; current visibility has not been checked.'
      : (pending.accept ? 'Candidate promoted to Curated' : 'Candidate rejected'));
    await Promise.all([loadMemoryInspect(), loadMemories()]);
  } catch (error) {
    showError(error.message || 'Review failed');
    button.disabled = false;
  }
}

function _explicitAddNoEffect(result, intent, candidateId) {
  return result && result.state === 'not_applied' && result.candidate_id === candidateId
    && Object.keys(result).length === 7 && ['conflict','policy_changed'].includes(result.reason)
    && ['expected_revision','expected_binding','review_key','action'].every(field => result[field] === intent?.[field]);
}
async function reviewExplicitAddCandidate(item, accept, button) {
  const ticket = _memoryActionTicket(), account = document.body?.dataset?.accountId;
  if (!account) { showError('Account is unavailable.'); return; }
  const key = `${account}:${item.id}`;
  if (_explicitAddEdits.has(key)) { showError('The original candidate edit outcome is unresolved.'); return; }
  let pending = _explicitAddReviews.get(key);
  const acknowledged = pending?.noEffect ? pending : null;
  if (acknowledged) pending = null;
  if (pending && pending.action !== (accept ? 'accept' : 'reject')) { showError('Check the original review status first.'); return; }
  button.disabled = true;
  try {
    if (!pending) {
      const originalResponse = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phase:'original'})});
      const original = await originalResponse.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
      if (!originalResponse.ok) throw new Error('Original review status unavailable.');
      if (original.state === 'terminal_erased' && original.candidate_id === item.id) {
        showToast('The original review was erased.'); return;
      }
      if (original.state === 'found') {
        const intent = original.original, receipt = original.receipt;
        if (!intent || intent.candidate_id !== item.id || !Number.isSafeInteger(intent.expected_revision)
            || !/^[a-f0-9]{64}$/.test(intent.expected_binding || '') || typeof intent.review_key !== 'string'
            || !intent.review_key || !['accept','reject'].includes(intent.action) || !receipt || receipt.candidate_id !== item.id
            || !['applied','rejected','unknown','not_applied'].includes(receipt.state)) throw new Error('Original review status unavailable.');
        if (receipt.state === 'not_applied') {
          if (!_explicitAddNoEffect(receipt, intent, item.id)) throw new Error('Original review status unavailable.');
          if (!acknowledged || !_explicitAddNoEffect(receipt, acknowledged, item.id)) {
            _explicitAddReviews.set(key, {...intent, noEffect:true});
            showError('The review was not applied. Refresh and review again.'); return;
          }
        } else {
        pending = {expected_revision:intent.expected_revision, expected_binding:intent.expected_binding,
                   review_key:intent.review_key, action:intent.action, issued:true};
        _explicitAddReviews.set(key, pending);
        if (receipt.state === 'applied' || receipt.state === 'rejected') {
          _explicitAddReviews.delete(key);
          showToast(receipt.state === 'applied' ? 'Original review was applied.' : 'Original review was rejected.');
          await Promise.all([loadMemoryInspect(), loadMemories()]); return;
        }
        }
      } else if (original.state !== 'absent') throw new Error('Original review status unavailable.');
    }
    if (!pending) {
      const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review-preview`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:accept ? 'accept' : 'reject'})});
      const preview = await response.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
      if (!response.ok || preview.review_kind !== 'explicit_add' || preview.candidate_id !== item.id
          || !Number.isSafeInteger(preview.expected_revision) || !/^[a-f0-9]{64}$/.test(preview.binding_sha256 || '')
          || !preview.proposal || preview.proposal.text !== (item.raw_content || item.content || '')) throw new Error('Candidate changed; refresh before review.');
      const recheckResponse = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phase:'original'})});
      const recheck = await recheckResponse.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
      const sameNoEffect = acknowledged && recheck.state === 'found'
        && _explicitAddNoEffect(recheck.receipt, acknowledged, item.id)
        && ['expected_revision','expected_binding','review_key','action'].every(field => recheck.original?.[field] === acknowledged[field]);
      if (!recheckResponse.ok || (recheck.state !== 'absent' && !sameNoEffect)) throw new Error('Original review changed; check its status.');
      pending = {expected_revision:preview.expected_revision, expected_binding:preview.binding_sha256, review_key:crypto.randomUUID(), action:accept ? 'accept' : 'reject'};
      _explicitAddReviews.set(key, pending);
    }
    const phase = pending.issued ? 'status' : 'apply';
    pending.issued = true;
    const response = await fetch(`/api/memory/candidate/${encodeURIComponent(item.id)}/review`, {
      method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({expected_revision:pending.expected_revision, expected_binding:pending.expected_binding,
                           review_key:pending.review_key, action:pending.action, phase})});
    const result = await response.json().catch(() => ({}));
    if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
    if (response.ok && _explicitAddNoEffect(result, pending, item.id)) {
      _explicitAddReviews.set(key, {...pending, noEffect:true});
      showError('The review was not applied. Refresh and review again.'); return;
    }
    if (response.ok && result.state === 'terminal_erased' && result.candidate_id === item.id) {
      _explicitAddReviews.delete(key); showToast('The original review was erased.'); return;
    }
    if (!response.ok || result.state !== (pending.action === 'accept' ? 'applied' : 'rejected') || result.candidate_id !== item.id)
      throw new Error('Original review outcome unavailable; select the same review action again to check status.');
    _explicitAddReviews.delete(key);
    showToast(pending.action === 'accept' ? 'Candidate promoted to Curated' : 'Candidate rejected');
    await Promise.all([loadMemoryInspect(), loadMemories()]);
  } catch (error) { if (_memoryActionCurrent(ticket)) showError(error.message || 'Original review unavailable.'); }
  finally { if (_memoryActionCurrent(ticket)) button.disabled = false; }
}

function renderMemoryInspect() {
  const list = document.getElementById('memory-inspect-list');
  const tier = document.getElementById('memory-inspect-tier')?.value || 'raw';
  if (!list) return;
  disposeMemoryBatchReview?.(); disposeMemoryBatchReview=null;
  list.replaceChildren();
  const preextracted = tier === 'candidate' ? preextractedInspect : [];
  if (!inspectedMemories.length && !preextracted.length && !(tier === 'candidate' && pairedInspect)) {
    const empty = document.createElement('div');
    empty.className = 'memory-empty';
    empty.textContent = `No ${tier === 'raw' ? 'raw trajectory' : tier} records.`;
    list.append(empty);
    return;
  }
  if (tier === 'candidate' && pairedInspect?.legacyState === 'unavailable') {
    const notice=document.createElement('p'); notice.textContent='Other candidate records are unavailable.'; list.append(notice);
  }
  if (tier === 'candidate' && manualAddInspect?.state === 'unavailable') {
    const notice=document.createElement('p'); notice.textContent='Original Add review history is unavailable.'; list.append(notice);
  }
  if (tier === 'candidate' && manualAddInspect?.partial) {
    const notice=document.createElement('p'); notice.textContent='Original Add review history is partial.'; list.append(notice);
  }
  if (tier === 'candidate' && pairedInspect?.state === 'unavailable') {
    const notice=document.createElement('p'); notice.textContent='Assistant interpretations are unavailable.'; list.append(notice);
  }
  if (tier === 'candidate' && pairedInspect?.partial) {
    const notice=document.createElement('p'); notice.textContent='Assistant interpretation list is partial; more results are available.'; list.append(notice);
  }
  preextracted.forEach(entry => list.append(_preextractedCard(entry)));
  inspectedMemories.forEach(item => {
    const card = document.createElement('div');
    card.className = 'memory-item';
    if (tier === 'candidate' && /^manualadd_[0-9a-f]{64}$/.test(item.id || '')) card.dataset.batchCandidate=item.id;
    const text = document.createElement('div');
    text.className = 'memory-item-text';
    text.textContent = _inspectText(item, tier);
    const meta = document.createElement('div');
    meta.className = 'admin-toggle-sub';
    meta.style.marginTop = '6px';
    meta.textContent = _inspectMeta(item, tier);
    card.append(text, meta);
    const inferred = _preextractedSource(item);
    if (inferred) {
      const badge = document.createElement('div'); badge.className = 'memory-inferred-line';
      const tag = document.createElement('span'); tag.className = 'memory-inferred-badge'; tag.textContent = 'inferred';
      badge.append(tag, document.createTextNode(` Source: ${_preextractedSourceLabel(inferred)}`));
      card.append(badge);
    }
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
    if (tier === 'candidate' && item.review_kind === 'explicit_add_original') {
      const check=document.createElement('button'); check.type='button'; check.className='memory-toolbar-btn'; check.textContent='Check original status';
      check.addEventListener('click',()=>reviewExplicitAddCandidate(item,item.original?.action !== 'reject',check)); card.append(check);
    }
    if (tier === 'candidate' && item.review_kind === 'assistant_interpretation') {
      const actions=document.createElement('div'); actions.style.cssText='display:flex;gap:6px;margin-top:8px';
      const pending=item._candidateReviewPending || item.original;
      if (pending || item.status === 'unknown') {
        const check=document.createElement('button'); check.type='button'; check.className='memory-toolbar-btn'; check.textContent='Check original status';
        check.disabled=!_pairedIntent(pending); check.addEventListener('click',()=>reviewPairedCandidate(item,'status',check)); actions.append(check);
      } else if (item.status === 'pending' && typeof item.content === 'string') {
        for (const [label,action] of [['Promote','accept'],['Reject','reject']]) {
          const button=document.createElement('button'); button.type='button'; button.className='memory-toolbar-btn'; button.textContent=label;
          button.addEventListener('click',()=>reviewPairedCandidate(item,action,button)); actions.append(button);
        }
      }
      card.append(actions);
    }
    if (tier === 'candidate' && item.status === 'pending' && item.review_kind !== 'assistant_interpretation') {
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
      if (_journalProposalText(item.content)) _journalSourceControl(card, item);
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
      if (current.kind === 'open_question' && current.status === 'open' && current.source_event_id === null &&
          Array.isArray(current.evidence_ids) && current.evidence_ids.length === 0 && current.activation_rationale?.question_context) {
        const review = current.activation_rationale.question_review;
        const disposition = review == null ? 'eligible' :
          (review.version === 1 && ['eligible','deferred','dismissed'].includes(review.disposition) ? review.disposition : null);
        const choices = disposition === 'eligible' ? [['Defer','question_defer'],['Dismiss','question_dismiss']] :
          (disposition ? [['Make eligible','question_reopen']] : []);
        for (const [label, action] of choices) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = label;
          button.addEventListener('click', async () => {
            try { const result = await _runVersionedAction(item, action); _publishedActionToast(result, 'Question updated'); }
            catch (error) { showError(error.message); }
          });
          actions.append(button);
        }
      }
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
            const result = await _runVersionedAction(item, 'retract', { reason: 'retracted by user' });
            _publishedActionToast(result, 'Memory retracted');
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
            const result = await _runVersionedAction(item, 'revert', { target_revision: prior.revision });
            _publishedActionToast(result, `Restored revision ${prior.revision}`);
          } catch (error) { showError(error.message); }
        });
        actions.append(revert);
      }
      if (actions.childNodes.length) card.append(actions);
    }
    list.append(card);
  });
  if (tier === 'candidate') {
    const scope=_pairedScope(), generation=memoryInspectGeneration, workspace=Storage.get(KEYS.WORKSPACE_ID,'') || '';
    disposeMemoryBatchReview=mountMemoryBatchReview({list,items:inspectedMemories,scope,current:()=>generation===memoryInspectGeneration && _pairedCurrent(scope) && (Storage.get(KEYS.WORKSPACE_ID,'') || '')===workspace && (document.getElementById('memory-inspect-tier')?.value || 'raw')==='candidate',onComplete:async()=>{await Promise.all([loadMemoryInspect(),loadMemories()]);}});
  }
  if (tier === 'candidate' && (manualAddInspect?.original_cursor || manualAddInspect?.terminal_cursor || manualAddInspect?.import_original_cursor || manualAddInspect?.import_terminal_cursor)) {
    const more=document.createElement('button'); more.type='button'; more.className='memory-toolbar-btn'; more.textContent='Load more original Add reviews';
    more.addEventListener('click',()=>_loadMoreManualAdd(more)); list.append(more);
  }
  if (tier === 'candidate' && ['ready','unavailable'].includes(pairedInspect?.state)
      && (pairedInspect.candidate_cursor || pairedInspect.intent_cursor)) {
    const more=document.createElement('button'); more.type='button'; more.className='memory-toolbar-btn'; more.textContent='Load more assistant interpretations';
    more.addEventListener('click',()=>_loadMorePaired(more)); list.append(more);
  }
}

function _mergeManualAddRows(rows, page, status) {
  const merged=new Map(rows.map(item=>[item.id,item]));
  for (const item of page.items || []) {
    if (!item || typeof item.id !== 'string' || !/^(?:manualadd|importreview)_[a-f0-9]{64}$/.test(item.id)
        || !['explicit_add_original','explicit_add_terminal'].includes(item.review_kind)
        || (status && item.status !== status)) continue;
    if (merged.has(item.id)) {
      if (item.review_kind === 'explicit_add_original' && item.status === 'unknown' && item.original)
        merged.set(item.id,{...merged.get(item.id),...item});
      continue;
    }
    merged.set(item.id,item);
  }
  return [...merged.values()];
}
async function _loadMoreManualAdd(button) {
  const page=manualAddInspect, scope=page?.scope, status=page?.filter, workspace=page?.workspace;
  const current=()=>manualAddInspect===page && _pairedCurrent(scope)
    && (document.getElementById('memory-inspect-tier')?.value || 'raw')==='candidate'
    && (document.getElementById('memory-inspect-status')?.value || '')===status
    && (Storage.get(KEYS.WORKSPACE_ID,'') || '')===workspace;
  if (!current()) return;
  button.disabled=true;
  try {
    const query=new URLSearchParams({tier:'candidate',manual_original_cursor:page.original_cursor || 'done',manual_terminal_cursor:page.terminal_cursor || 'done',import_original_cursor:page.import_original_cursor || 'done',import_terminal_cursor:page.import_terminal_cursor || 'done',paired_candidate_cursor:'done',paired_intent_cursor:'done'});
    if (status) query.set('status',status);
    const response=await fetch(`/api/memory/inspect?${query}`,{credentials:'same-origin'});
    const data=await response.json().catch(()=>({})); if (!current()) return;
    const next=data.manual_add;
    if (!response.ok || !['ready','unavailable'].includes(next?.state) || !Array.isArray(next.items)) throw new Error('More original reviews are unavailable.');
    if (next.state === 'unavailable') {
      page.state='unavailable'; page.partial=true;
      inspectedMemories=_mergeManualAddRows(inspectedMemories,next,status); renderMemoryInspect(); return;
    }
    const before=JSON.stringify([page.original_cursor,page.terminal_cursor,page.import_original_cursor,page.import_terminal_cursor]);
    Object.assign(page,{state:next.state,original_cursor:next.original_cursor || null,terminal_cursor:next.terminal_cursor || null,import_original_cursor:next.import_original_cursor || null,import_terminal_cursor:next.import_terminal_cursor || null,partial:!!next.partial});
    if ((page.original_cursor || page.terminal_cursor || page.import_original_cursor || page.import_terminal_cursor) && before===JSON.stringify([page.original_cursor,page.terminal_cursor,page.import_original_cursor,page.import_terminal_cursor])) throw new Error('More original reviews are unavailable.');
    inspectedMemories=_mergeManualAddRows(inspectedMemories,next,status); renderMemoryInspect();
  } catch (error) { if (current()) { showError(error.message || 'More original reviews are unavailable.'); button.disabled=false; } }
}

async function _loadMorePaired(button) {
  const page=pairedInspect, scope=page?.scope, status=page?.filter, workspace=page?.workspace;
  const current=()=>pairedInspect===page && _pairedCurrent(scope)
    && (document.getElementById('memory-inspect-tier')?.value || 'raw')==='candidate'
    && (document.getElementById('memory-inspect-status')?.value || '')===status
    && (Storage.get(KEYS.WORKSPACE_ID,'') || '')===workspace;
  if (!current()) return;
  button.disabled=true;
  try {
    const query=new URLSearchParams({tier:'candidate', paired_candidate_cursor:page.candidate_cursor || 'done', paired_intent_cursor:page.intent_cursor || 'done'});
    if (status) query.set('status',status);
    const response=await fetch(`/api/memory/inspect?${query}`,{credentials:'same-origin'});
    const data=await response.json().catch(()=>({}));
    if (!current()) return;
    const next=data.paired;
    if (!response.ok || !['ready','unavailable'].includes(next?.state) || !Array.isArray(next.items)) throw new Error('More assistant interpretations are unavailable.');
    const merged=new Map(inspectedMemories.map(item=>[item.id,item]));
    for (const item of next.items) {
      if (item && typeof item.id==='string' && item.review_kind==='assistant_interpretation'
          && ['pending','unknown','accepted','rejected'].includes(item.status) && (!status || item.status===status)
          && (next.state === 'ready' || item.original))
        merged.set(item.id,_mergePairedRow(merged.get(item.id),item));
    }
    const before=JSON.stringify([page.candidate_cursor,page.intent_cursor]);
    Object.assign(page,{state:next.state,candidate_cursor:next.candidate_cursor || null,intent_cursor:next.intent_cursor || null,partial:!!next.partial});
    if ((page.candidate_cursor || page.intent_cursor) && before===JSON.stringify([page.candidate_cursor,page.intent_cursor]))
      throw new Error('More assistant interpretations are unavailable.');
    inspectedMemories=[...merged.values()]; renderMemoryInspect();
  } catch (error) { if (current()) { showError(error.message || 'More results are unavailable.'); button.disabled=false; } }
}

// Text of the quality line from a /api/memory/quality reply: states only fields the reply carries.
export function memoryQualityLine(quality) {
  const isObj=value=>value && typeof value === 'object' && !Array.isArray(value);
  const num=value=>typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  if (!isObj(quality)) return 'Memory quality unavailable.';
  const counts=isObj(quality.counts) ? quality.counts : {};
  const tier=isObj(counts.by_tier) ? counts.by_tier : {};
  const graph=isObj(quality.graph) ? quality.graph : {};
  const parts=[];
  const add=(label,value)=>{ if (value !== null) parts.push(`${label} ${value}`); };
  add('Memories',num(tier.curated));
  add('Awaiting review',num(tier.candidates_pending ?? counts.candidates_pending));
  add('Raw',num(tier.raw));
  if (isObj(counts.by_kind)) {
    const kinds=Object.entries(counts.by_kind).filter(([,n])=>num(n) !== null && n > 0)
      .sort((a,b)=>b[1]-a[1] || (a[0] < b[0] ? -1 : 1)).slice(0,3);
    if (kinds.length) parts.push(`Kinds ${kinds.map(([k,n])=>`${k} ${n}`).join(', ')}`);
  }
  if (typeof graph.integrity_ok === 'boolean') parts.push(graph.integrity_ok ? 'Index healthy' : 'Index needs rebuild');
  add('Index cues',num(graph.visible_cues));
  return parts.length ? parts.join(' · ') : 'Memory quality unavailable.';
}

export async function loadMemoryInspect({ isCurrent = () => true } = {}) {
  if (!isCurrent()) return;
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
  pairedInspect=null;
  manualAddInspect=null;
  const pairedScope=_pairedScope(), pairedWorkspace=Storage.get(KEYS.WORKSPACE_ID,'') || '';
  const generation=++memoryInspectGeneration;
  const loadCurrent=()=>generation===memoryInspectGeneration && isCurrent()
    && (pairedScope ? _pairedCurrent(pairedScope) : !_pairedScope())
    && (Storage.get(KEYS.WORKSPACE_ID,'') || '')===pairedWorkspace
    && (document.getElementById('memory-inspect-tier')?.value || 'raw')===tier
    && (!['candidate','history'].includes(tier) || (statusSelect?.value || '')===status);
  if (list) list.textContent = 'Loading…';
  const qualityEl=document.getElementById('memory-quality');
  if (qualityEl) qualityEl.textContent='Memory quality pending…';
  if (qualityEl && !document.getElementById('memory-journal-coverage')) { const box=document.createElement('div'); box.id='memory-journal-coverage'; qualityEl.after(box); mountJournalCoverage(box,()=>sessionModule?.getCurrentSessionId?.()||''); }
  if (qualityEl && !document.getElementById('memory-shadow-ranking')) { const box=document.createElement('div'); box.id='memory-shadow-ranking'; qualityEl.after(box); mountShadowRanking(box); }
  void Promise.resolve().then(()=>fetch('/api/memory/quality', {credentials:'same-origin'}))
    .then(async response=>response?.ok ? await response.json().catch(()=>null) : null)
    .catch(()=>null).then(quality=>{
      if (!loadCurrent() || !qualityEl) return;
      qualityEl.textContent=memoryQualityLine(quality);
    });
  try {
    const tierResponse=await fetch(`/api/memory/inspect?tier=${encodeURIComponent(tier)}${status ? `&status=${encodeURIComponent(status)}` : ''}`, {credentials:'same-origin'});
    if (!tierResponse.ok) throw new Error('Memory inspection unavailable');
    const tierData=await tierResponse.json();
    if (!loadCurrent()) return;
    inspectedMemories = Array.isArray(tierData.items) ? tierData.items : [];
    pairedInspect = tier === 'candidate' && tierData.paired && typeof tierData.paired === 'object'
      ? (pairedScope ? tierData.paired : {state:'unavailable'}) : null;
    if (pairedInspect) Object.assign(pairedInspect,{scope:pairedScope,workspace:pairedWorkspace,filter:status,legacyState:tierData.legacy_state || null});
    if (['ready','unavailable'].includes(pairedInspect?.state) && Array.isArray(pairedInspect.items)) {
      const merged=new Map(inspectedMemories.map(item=>[item.id,item]));
      for (const item of pairedInspect.items) {
        if (item && typeof item.id === 'string' && item.review_kind === 'assistant_interpretation'
            && ['pending','unknown','accepted','rejected'].includes(item.status) && (!status || item.status === status)
            && (pairedInspect.state === 'ready' || item.original))
          merged.set(item.id,_mergePairedRow(merged.get(item.id),item));
      }
      inspectedMemories=[...merged.values()];
    }
    manualAddInspect = tier === 'candidate' && tierData.manual_add && typeof tierData.manual_add === 'object'
      ? (pairedScope ? tierData.manual_add : {state:'unavailable',items:[]}) : null;
    if (manualAddInspect) {
      Object.assign(manualAddInspect,{scope:pairedScope,workspace:pairedWorkspace,filter:status});
      if (Array.isArray(manualAddInspect.items)) inspectedMemories=_mergeManualAddRows(inspectedMemories,manualAddInspect,status);
    }
    preextractedInspect = [];
    if (tier === 'candidate' && (!status || status === 'pending')) {
      try {
        const response = await fetch('/api/memory/candidates/preextracted?limit=50', { credentials: 'same-origin' });
        const data = response.ok ? await response.json() : null;
        if (!loadCurrent()) return;
        if (Array.isArray(data?.items)) preextractedInspect = data.items.filter(entry => typeof entry?.candidate_id === 'string' && _preextractedSource(entry));
      } catch (_) { /* guarded store only; the rest of the list is unaffected */ }
    }
    renderMemoryInspect();
  } catch (error) {
    if (!loadCurrent()) return;
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
  const editBtn = document.getElementById('memory-bulk-edit');
  if (editBtn) editBtn.disabled = selectedIds.size === 0 || selectedIds.size > 100;
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
  const action = _memoryActionTicket();
  const ids = [...selectedIds];
  const count = selectedIds.size;
  if (!await uiModule.styledConfirm(`Delete ${count} ${count === 1 ? 'memory' : 'memories'}?`, { confirmText: 'Delete', danger: true })) return;
  if (!_memoryActionCurrent(action)) return;
  disposeMemoryGraph({ clear: false, reason: 'forget' });
  action.loadId = graphState.loadId;

  let deleted = 0;
  const deletedIds = [];
  for (const id of ids) {
    if (!_memoryActionCurrent(action)) return;
    try {
      const res = await fetch(`${window.location.origin}/api/memory/${id}`, { method: 'DELETE' });
      if (!_memoryActionCurrent(action)) return;
      if (res.ok) {
        deleted++;
        deletedIds.push(id);
      } else { const failure = await _memoryDeleteFailure(res); if (_memoryActionCurrent(action)) showError(failure); }
    } catch (e) {
      if (!_memoryActionCurrent(action)) return;
      console.error('Failed to delete memory:', id, e);
    }
  }
  if (!_memoryActionCurrent(action)) return;
  _removeForgottenGraphNodes(deletedIds);

  await animateMemoryRemoval(deletedIds);
  if (!_memoryActionCurrent(action)) return;
  exitSelectMode();
  await loadMemories({ isCurrent: () => _memoryActionCurrent(action) });
  if (!_memoryActionCurrent(action)) return;
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
  canvas: null,
  wiring: null,
  controller: null,
  receipt: {},
  ready: false,
  detailRevision: 0,
  pending: new Set(),
  workbench: null,
};

function _graphContext() {
  return JSON.stringify({
    account: document.body?.dataset.accountId || Storage.get('openclank-auth-owner', '') || '',
    // View lifetime only: ordinary-memory graph retains its backend scope.
    workspace: Storage.get(KEYS.WORKSPACE_ID, '') || '',
  });
}

function _graphMounted(canvas) {
  return canvas?.isConnected && document.getElementById('memory-graph-canvas') === canvas
    && !canvas.closest('.hidden, [hidden]');
}

export function disposeMemoryGraph({ clear = true, reason = 'unmounted' } = {}) {
  graphState.loadId += 1;
  graphState.controller?.abort();
  graphState.controller = new AbortController();
  graphState.pending.clear();
  graphState.detailRevision += 1;
  if (clear) {
    graphState.wiring?.abort();
    graphState.wiredCanvas = null;
    graphState.ready = false;
    graphState.receipt = {};
    graphState.nodes = [];
    graphState.edges = [];
    graphState.positions = new Map();
    graphState.selected = null;
    graphState.totals = { nodes: null, edges: null };
  }
  if (graphState.workbench) {
    graphState.workbench.results.hidden = true;
    graphState.workbench.results.replaceChildren();
    _renderGraphWorkbench();
  }
  if (graphState.canvas) {
    graphState.canvas.dataset.graphState = reason;
    graphState.canvas.dataset.graphPending = '0';
  }
}

function _graphTicket(op, { detail = false } = {}) {
  const ticket = { op, loadId: graphState.loadId, canvas: graphState.canvas,
    context: _graphContext(), receipt: JSON.stringify(graphState.receipt),
    detailRevision: detail ? ++graphState.detailRevision : null,
    interactionRevision: graphState.detailRevision,
    signal: graphState.controller.signal };
  graphState.pending.add(ticket);
  ticket.canvas.dataset.graphPending = String(graphState.pending.size);
  return ticket;
}

function _graphTicketCurrent(ticket) {
  return ticket.loadId === graphState.loadId && ticket.canvas === graphState.canvas
    && _graphMounted(ticket.canvas) && ticket.context === _graphContext()
    && ticket.receipt === JSON.stringify(graphState.receipt)
    && (ticket.detailRevision === null || ticket.detailRevision === graphState.detailRevision);
}

function _graphReceipt(result) {
  const source = result?.receipt || result?.scope_receipt || result || {};
  const receipt = {};
  for (const key of ['generation', 'head_generation', 'admission_epoch', 'policy_generation', 'checkout_id', 'workspace_id', 'registration_id', 'source_snapshot_id', 'config_fingerprint']) {
    if (Object.hasOwn(source, key)) receipt[key] = source[key];
  }
  return receipt; // No constant/invented generation for legacy producers.
}

function _graphAccept(ticket, result) {
  if (!_graphTicketCurrent(ticket)) return false;
  if (ticket.op !== 'overview' && JSON.stringify(_graphReceipt(result)) !== ticket.receipt) {
    disposeMemoryGraph({ reason: 'stale' });
    _drawGraph(ticket.canvas);
    _renderGraphCounts();
    const detail = document.getElementById('memory-graph-detail');
    if (detail) detail.textContent = 'Graph generation changed — reload this view.';
    return false;
  }
  if (ticket.op !== 'expand' || ticket.interactionRevision === graphState.detailRevision) ticket.canvas.dataset.graphState = 'ready';
  return true;
}

function _graphFinish(ticket) {
  if (ticket.loadId !== graphState.loadId || ticket.canvas !== graphState.canvas
      || !_graphMounted(ticket.canvas) || ticket.context !== _graphContext()) return;
  graphState.pending.delete(ticket);
  ticket.canvas.dataset.graphPending = String(graphState.pending.size);
}

function _graphFailure(ticket, error, message) {
  if (!_graphTicketCurrent(ticket)) return;
  if (ticket.op === 'expand' && ticket.interactionRevision !== graphState.detailRevision) return;
  ticket.canvas.dataset.graphState = error?.name === 'AbortError' ? 'cancelled' : 'error';
  const detail = document.getElementById('memory-graph-detail');
  if (detail) detail.textContent = error?.name === 'AbortError' ? 'Graph request cancelled.' : message;
}

async function _graphApi(params, ticket) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`/api/memory/graph?${query}`, { credentials: 'same-origin', signal: ticket.signal });
  if (!response.ok) throw new Error(`graph ${params.op} failed`);
  return response.json();
}

export async function loadMemoryGraph({ tagFilter = null } = {}) {
  const canvas = document.getElementById('memory-graph-canvas');
  if (!_graphMounted(canvas)) return;
  disposeMemoryGraph({ reason: 'loading' });
  graphState.canvas = canvas;
  _wireGraphCanvas(canvas);
  _wireGraphWorkbench(canvas);
  canvas.dataset.graphState = 'loading';
  _drawGraph(canvas); _renderGraphCounts();
  const loadingDetail = document.getElementById('memory-graph-detail');
  if (loadingDetail) loadingDetail.textContent = 'Loading graph…';
  const ticket = _graphTicket('overview');
  try {
    const overview = await _graphApi({ op: 'overview', limit: 120 }, ticket);
    if (!_graphAccept(ticket, overview)) return;
    _graphFinish(ticket);
    graphState.nodes = [...new Map((overview.nodes || []).filter(n => n?.id).map(n => [n.id, n])).values()];
    graphState.edges = [...new Map((overview.edges || []).filter(e => e?.id).map(e => [e.id, e])).values()];
    graphState.totals = { nodes: Number.isSafeInteger(overview.node_total) ? overview.node_total : null,
      edges: Number.isSafeInteger(overview.edge_total) ? overview.edge_total : null };
    graphState.receipt = _graphReceipt(overview);
    graphState.ready = true;
    canvas.dataset.graphState = 'ready';
    graphState.selected = null;
    graphState.tagFilter = typeof tagFilter === 'string' && tagFilter ? tagFilter : null;
    _resetGraphViewport();
    _relayoutGraph(canvas);
    _renderGraphTags();
    _renderGraphCounts();
    _renderGraphDetail(null);
  } catch (error) {
    _graphFailure(ticket, error, 'Graph unavailable — is the Memery provider running?');
  } finally {
    _graphFinish(ticket);
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
    ctx.fillText(canvas.dataset.graphState === 'loading' ? 'Loading graph…' : 'No saved memories here yet', canvas.width / 2, canvas.height / 2 - 10);
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
  if (graphState.wiredCanvas === canvas && !graphState.wiring?.signal.aborted) return;
  graphState.wiring?.abort();
  graphState.wiredCanvas = canvas;
  graphState.wiring = new AbortController();
  const listen = (element, type, handler, options = {}) => element.addEventListener(type, handler,
    { ...options, signal: graphState.wiring.signal });

  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  let moved = false;

  listen(canvas, 'pointerdown', (e) => {
    if (!_graphMounted(canvas) || !graphState.ready) return;
    dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = 'grabbing';
  });
  listen(canvas, 'pointermove', (e) => {
    if (!dragging || !_graphMounted(canvas)) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    graphState.offsetX += dx * (canvas.width / canvas.getBoundingClientRect().width);
    graphState.offsetY += dy * (canvas.height / canvas.getBoundingClientRect().height);
    lastX = e.clientX; lastY = e.clientY;
    _drawGraph(canvas);
  });
  listen(canvas, 'pointerup', async (e) => {
    if (!_graphMounted(canvas) || !graphState.ready) return;
    dragging = false;
    canvas.style.cursor = 'grab';
    if (moved) return; // it was a pan, not a click
    const point = _toGraphCoords(canvas, e.clientX, e.clientY);
    const hit = hitTest(graphState.positions, point.x, point.y, 14 / graphState.scale);
    if (!hit) { _graphSelectNode(null); return; }
    if (e.shiftKey && graphState.selected && graphState.selected !== hit) {
      await _traceBetween(graphState.selected, hit);
      return;
    }
    if (graphState.selected === hit) {
      await _expandNode(hit, canvas);
      return;
    }
    _graphSelectNode(hit);
  });
  listen(canvas, 'wheel', (e) => {
    if (!_graphMounted(canvas) || !graphState.ready) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    graphState.scale = Math.max(0.3, Math.min(4, graphState.scale * factor));
    _drawGraph(canvas);
  }, { passive: false });

  const search = document.getElementById('memory-graph-search');
  if (search) {
    listen(search, 'keydown', async (e) => {
      if (e.key !== 'Enter') return;
      if (!_graphMounted(canvas) || !graphState.ready) return;
      const query = search.value.trim();
      if (!query) return;
      const ticket = _graphTicket('cues', { detail: true });
      try {
        const cues = await _graphApi({ op: 'cues', query, limit: 5 }, ticket);
        if (!_graphAccept(ticket, cues)) return;
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
        _renderGraphCounts();
      } catch (error) { _graphFailure(ticket, error, 'Graph search failed.'); }
      finally { _graphFinish(ticket); }
    });
  }
}

async function _expandNode(nodeId, canvas) {
  if (!_graphMounted(canvas) || !graphState.ready) return;
  const ticket = _graphTicket('expand');
  try {
    const result = await _graphApi({ op: 'expand', node: nodeId, limit: 25 }, ticket);
    if (!_graphAccept(ticket, result)) return;
    mergeExpansion(graphState, result.hits || []);
    _relayoutGraph(canvas);
    _renderGraphTags();
    if (ticket.interactionRevision === graphState.detailRevision) _renderGraphDetail(graphState.selected);
    _renderGraphCounts();
  } catch (error) { _graphFailure(ticket, error, 'Graph expansion failed.'); }
  finally { _graphFinish(ticket); }
}

async function _traceBetween(fromId, toId) {
  const detail = document.getElementById('memory-graph-detail');
  if (!detail) return;
  if (!graphState.ready) return;
  const ticket = _graphTicket('trace', { detail: true });
  try {
    const result = await _graphApi({ op: 'trace', node: fromId, to_node: toId, limit: 10 }, ticket);
    if (!_graphAccept(ticket, result)) return;
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
  } catch (error) { _graphFailure(ticket, error, 'Trace failed.'); }
  finally { _graphFinish(ticket); }
}

function _nodeName(nodeId) {
  const node = graphState.nodes.find((n) => n.id === nodeId);
  return node ? (node.label || node.name) : nodeId;
}

const MEMORY_GRAPH_UUID = /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i;

function _graphElement(tag, text, ...children) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  node.append(...children);
  return node;
}

function _graphControl(text, handler) {
  const button = _graphElement('button', text);
  button.type = 'button';
  button.className = 'memory-cat-chip';
  button.addEventListener('click', handler);
  return button;
}

function _graphSelectNode(id) {
  if (!graphState.ready || id!==null && !graphState.nodes.some(node => node.id === id)) return;
  graphState.selected = id;
  graphState.detailRevision += 1;
  const view = graphState.workbench;
  if (view) { view.results.hidden = true; view.results.replaceChildren(); }
  if (graphState.canvas) _drawGraph(graphState.canvas);
  _renderGraphDetail(id);
}

function _wireGraphWorkbench(canvas) {
  graphState.workbench?.controls.remove();
  graphState.workbench?.panel.remove();
  graphState.workbench?.results.remove();
  const controls = _graphElement('div');
  controls.setAttribute('aria-label', 'Memory graph views');
  controls.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;';
  const panel = _graphElement('section');
  panel.style.cssText = 'flex:1;min-width:0;overflow:auto;max-height:420px;';
  panel.tabIndex = -1;
  const results = _graphElement('section');
  results.hidden = true;
  results.style.cssText = 'overflow:auto;max-height:420px;margin-top:8px;';
  results.setAttribute('aria-label', 'Memory graph reconstruction results');
  results.tabIndex = -1;
  const search = _graphElement('input');
  search.type = 'search';
  search.maxLength = 1024;
  search.placeholder = 'Find related memories';
  search.setAttribute('aria-label', 'Graph reconstruction query');
  const intent = _graphElement('select');
  intent.setAttribute('aria-label', 'Graph reconstruction intent');
  for (const label of ['General', 'Temporal', 'Causal', 'Entity']) {
    const option = _graphElement('option', label);
    option.value = label.toLowerCase();
    intent.append(option);
  }
  const community = _graphElement('select');
  community.setAttribute('aria-label', 'Community context');
  for (const [value, label] of [['focal', 'Around the query'], ['broad', 'Across retained memory'], ['off', 'Individual evidence']]) {
    const option = _graphElement('option', label);
    option.value = value;
    community.append(option);
  }
  const adaptive = _graphElement('input');
  adaptive.type = 'checkbox';
  adaptive.checked = true;
  const adaptiveLabel = _graphElement('label', '', adaptive, _graphElement('span', 'Stop when evidence stabilizes'));
  const reconstruct = _graphControl('Reconstruct', () => void _reconstructMemoryGraph(search.value, intent.value, community.value, adaptive.checked));
  const modes = new Map();
  const view = graphState.workbench = {canvas, controls, panel, results, modes, mode:'graph', page:0, search, community, reconstruct};
  for (const [id, title] of [['graph', 'Graph'], ['table', 'Table'], ['claims', 'Claims & support']]) {
    const button = _graphControl(title, () => {
      if (graphState.workbench !== view) return;
      view.mode = id;
      view.page = 0;
      _renderGraphWorkbench();
      if (id !== 'graph') panel.focus({preventScroll:true});
    });
    modes.set(id, button);
    controls.append(button);
  }
  const clearReconstruction = () => {
    graphState.detailRevision++;
    results.hidden = true;
    results.replaceChildren();
    _updateGraphReconstructControl();
  };
  search.addEventListener('input', clearReconstruction);
  for (const control of [intent, community, adaptive]) control.addEventListener('change', clearReconstruction);
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !reconstruct.disabled) {
      event.preventDefault();
      reconstruct.click();
    }
  });
  controls.append(search, intent, community, adaptiveLabel, reconstruct);
  canvas.parentElement.before(controls);
  canvas.after(panel);
  canvas.parentElement.after(results);
  _renderGraphWorkbench();
}

function _updateGraphReconstructControl() {
  const view = graphState.workbench;
  if (view) view.reconstruct.disabled = !graphState.ready ||
    view.community.value !== 'broad' && !view.search.value.trim() && !MEMORY_GRAPH_UUID.test(graphState.selected || '');
}

function _renderGraphWorkbench() {
  const view = graphState.workbench;
  if (!view || !_graphMounted(view.canvas)) return;
  _updateGraphReconstructControl();
  const {mode, panel} = view;
  const focused = panel.contains(document.activeElement) ? document.activeElement.dataset.memoryGraphItem : null;
  for (const [id, button] of view.modes) button.setAttribute('aria-pressed', String(id === mode));
  view.canvas.style.display = mode === 'graph' ? '' : 'none';
  panel.hidden = mode === 'graph';
  if (mode === 'graph') { _drawGraph(view.canvas); return; }
  if (!graphState.ready) { panel.textContent = 'Memory graph data is unavailable.'; return; }
  if (mode === 'claims') {
    panel.setAttribute('aria-label', 'Loaded claims and typed supporting evidence');
    const known = new Map(graphState.nodes.map(node => [node.id, node]));
    const supportKinds = new Set(['evidence', 'support']);
    const typed = graphState.edges.flatMap(edge => {
      if (!['supports', 'contradicts'].includes(edge.tag) ||
          graphState.tagFilter && graphState.tagFilter !== edge.tag) return [];
      const from = known.get(edge.src_id), to = known.get(edge.dst_id);
      if (to?.kind !== 'claim' || !supportKinds.has(from?.kind)) return [];
      return [{from, to, edge}];
    });
    const content = _graphElement('p', 'Loaded evidence → claim · supports or contradicts. Recorded edge trust is shown when available.');
    const rows = typed.slice(0, 60).map(({from, to, edge}) => {
      const row = _graphElement('li', '',
        _graphControl(_nodeName(from.id), () => _graphSelectNode(from.id)),
        _graphElement('span', ' ' + edge.tag + ' → '),
        _graphControl(_nodeName(to.id), () => _graphSelectNode(to.id)));
      if (Number.isFinite(edge.trust)) row.append(_graphElement('span', ' · edge trust ' + edge.trust));
      if (typeof edge.fact === 'string' && edge.fact) row.append(_graphElement('p', edge.fact.slice(0, 400)));
      return row;
    });
    panel.replaceChildren(content, rows.length ? _graphElement('ul', '', ...rows)
      : _graphElement('p', 'No typed support or contradiction is available in this loaded view.'));
    if (typed.length > rows.length) panel.append(_graphElement('p', (typed.length - rows.length) + ' typed relationships beyond the display limit'));
    return;
  }
  panel.setAttribute('aria-label', 'Loaded Memory graph table');
  const columns = _graphElement('tr');
  for (const label of ['Memory', 'Kind', 'Loaded connections']) {
    const column = _graphElement('th', label);
    column.scope = 'col';
    columns.append(column);
  }
  const degrees = new Map();
  for (const edge of graphState.edges) {
    if (graphState.tagFilter && edge.tag !== graphState.tagFilter) continue;
    for (const id of new Set([edge.src_id, edge.dst_id])) degrees.set(id, (degrees.get(id) || 0) + 1);
  }
  const nodes = graphState.nodes;
  view.page = Math.max(0, Math.min(view.page, Math.ceil(nodes.length / 40)-1));
  const offset = view.page * 40;
  const rows = nodes.slice(offset, offset + 40).map(node => {
    const pick = _graphControl(_nodeName(node.id), () => _graphSelectNode(node.id));
    pick.dataset.memoryGraphItem = node.id;
    pick.setAttribute('aria-pressed', String(graphState.selected === node.id));
    const name = _graphElement('th', '', pick);
    name.scope = 'row';
    return _graphElement('tr', '', name, _graphElement('td', node.kind), _graphElement('td', String(degrees.get(node.id) || 0)));
  });
  const previous = _graphControl('Previous', () => { view.page -= 1; _renderGraphWorkbench(); panel.focus(); });
  const next = _graphControl('Next', () => { view.page += 1; _renderGraphWorkbench(); panel.focus(); });
  previous.disabled = offset === 0;
  next.disabled = offset + 40 >= nodes.length;
  const status = _graphElement('p', rows.length ? (offset + 1) + '–' + (offset + rows.length) + ' of ' + nodes.length + ' loaded memories' : 'No loaded memories match the current graph filter.');
  panel.replaceChildren(status, _graphElement('table', '', _graphElement('thead', '', columns), _graphElement('tbody', '', ...rows)), previous, next);
  if (focused) [...panel.querySelectorAll('[data-memory-graph-item]')].find(node => node.dataset.memoryGraphItem === focused)?.focus({preventScroll:true});
}

function _reconstructionNode(id) {
  const known = graphState.nodes.some(node => node.id === id);
  if (known) return _graphControl(_nodeName(id), () => _graphSelectNode(id));
  const button = _graphControl('Open memory ' + id.slice(0, 8), async () => {
    const ticket = _graphTicket('fetch', {detail:true});
    button.disabled = true;
    try {
      const result = await _graphApi({op:'fetch', node:id}, ticket);
      if (!_graphTicketCurrent(ticket)) return;
      if (result?.node?.id !== id) {
        button.textContent = 'Memory unavailable';
        return;
      }
      if (!graphState.nodes.some(node => node.id === id)) {
        graphState.nodes.push(result.node);
        _relayoutGraph(ticket.canvas);
        _renderGraphCounts();
      }
      _graphSelectNode(id);
    } catch {
      if (_graphTicketCurrent(ticket)) button.textContent = 'Memory unavailable';
    } finally {
      _graphFinish(ticket);
      button.disabled = false;
    }
  });
  return button;
}

async function _reconstructMemoryGraph(query, intent, community, adaptive) {
  const view = graphState.workbench, search = query.trim();
  if (!view || !graphState.ready || community !== 'broad' && !search && !MEMORY_GRAPH_UUID.test(graphState.selected || '')) return;
  const ticket = _graphTicket('reconstruct', {detail:true});
  const pending = _graphElement('p', 'Reading ranked native memory graph evidence…');
  pending.setAttribute('role', 'status');
  view.results.replaceChildren(pending);
  view.results.hidden = false;
  view.results.focus({preventScroll:true});
  const body = {intent, community, adaptive};
  if (search) body.query = search;
  else if (MEMORY_GRAPH_UUID.test(graphState.selected || '')) body.seed_ids = [graphState.selected];
  if (graphState.tagFilter) body.tag = graphState.tagFilter;
  const owns = () => graphState.workbench === view && _graphTicketCurrent(ticket);
  try {
    const response = await fetch('/api/memory/graph/reconstruct', {
      method:'POST', credentials:'same-origin', signal:ticket.signal,
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    if (!response.ok) throw new Error();
    const value = await response.json();
    if (!owns()) return;
    if (value?.version !== 1 || !Array.isArray(value.nodes) ||
        !Array.isArray(value.steps) || !Array.isArray(value.omitted)) throw new Error();
    if (value.communities && (value.communities.mode !== community ||
        !Array.isArray(value.communities.groups) || value.communities.groups.length > 6)) throw new Error();
    const regions = (value.communities?.groups || []).flatMap(group => {
      if (!/^[a-f0-9]{64}$/.test(group?.id || '') || !Array.isArray(group.members) ||
          !Array.isArray(group.anchor_ids) || !Array.isArray(group.witnesses)) return [];
      const links = group.witnesses.slice(0, 12).flatMap(edge =>
        MEMORY_GRAPH_UUID.test(edge?.from || '') && MEMORY_GRAPH_UUID.test(edge?.to || '') &&
        typeof edge.tag === 'string' ? [_graphElement('li', '', _reconstructionNode(edge.from),
          _graphElement('span', ' → ' + edge.tag.slice(0, 128) + ' → '), _reconstructionNode(edge.to))] : []);
      if (!links.length) return [];
      const count = group.members.length + ' memories · ' + group.witnesses.length + ' recorded relationships';
      const details = _graphElement('details', '', _graphElement('summary', count),
        _graphElement('ul', '', ...links));
      if (group.witnesses.length > links.length) details.append(_graphElement('small', 'Showing first ' + links.length + ' relationships'));
      return [details];
    });
    const rows = value.nodes.slice(0, 96).flatMap(row => {
      if (!MEMORY_GRAPH_UUID.test(row?.id || '') || typeof row.kind !== 'string' ||
          !Number.isFinite(row.score) || !Array.isArray(row.lanes)) return [];
      const label = _reconstructionNode(row.id);
      const ranks = row.lanes.slice(0, 8).filter(lane => typeof lane?.lane === 'string' && Number.isSafeInteger(lane.rank))
        .map(lane => lane.lane.slice(0, 60) + ' #' + lane.rank);
      const item = _graphElement('li', '', label, _graphElement('span', ' · ' + row.kind.slice(0, 80) + ' · rank score ' + row.score.toFixed(3) + (ranks.length ? ' · ' + ranks.join(', ') : '')));
      const witness = row.witness;
      if (MEMORY_GRAPH_UUID.test(witness?.from || '') && MEMORY_GRAPH_UUID.test(witness?.to || '') && typeof witness.tag === 'string') {
        item.append(_graphElement('small', ' · recorded edge ' + _nodeName(witness.from) + ' → ' + witness.tag.slice(0, 128) + ' → ' + _nodeName(witness.to)));
      }
      return [item];
    });
    const limits = value.omitted.slice(0, 16).filter(item => typeof item === 'string').map(item => item.slice(0, 100));
    const stops = {
      adaptive_evidence_stable:'Stopped after two rounds added little evidence',
      frontier_exhausted:'No more connected evidence in this traversal',
      edge_limit:'Reached the relationship limit', step_limit:'Reached the step limit',
      node_limit:'Reached the memory limit', no_evidence:'No matching evidence',
    };
    if (typeof value.stop_reason === 'string') limits.push(stops[value.stop_reason] || value.stop_reason.slice(0, 100));
    view.results.replaceChildren(_graphElement('h4', 'Native memory graph reconstruction'),
      _graphElement('p', value.steps.length + ' bounded traversal steps' + (limits.length ? ' · ' + limits.join(', ') : '')),
      ...(community !== 'off' ? [_graphElement('h5', community === 'broad' ? 'Communities across retained memory' : 'Communities near the query'),
        ...(regions.length ? regions : [_graphElement('p', 'No community had enough recorded relationships in this search.')])] : []),
      rows.length ? _graphElement('ol', '', ...rows) : _graphElement('p', 'No individual evidence matched.'));
  } catch {
    if (owns()) view.results.replaceChildren(_graphElement('p', 'Native memory graph reconstruction is unavailable.'));
  } finally {
    _graphFinish(ticket);
  }
}

function _renderGraphDetail(nodeId) {
  if (graphState.workbench) _renderGraphWorkbench();
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
  if (_isCodeGraphNode(node)) {
    const forget = document.createElement('button');
    forget.type = 'button';
    forget.textContent = 'Forget node';
    forget.addEventListener('click', () => deleteMemory(node.id));
    detail.appendChild(forget);
  }
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
      if (graphState.workbench) {
        graphState.workbench.page=0;
        graphState.workbench.results.hidden=true;
        graphState.workbench.results.replaceChildren();
      }
      graphState.detailRevision += 1;
      graphState.tagFilter = graphState.tagFilter === tag ? null : tag;
      _renderGraphTags();
      const canvas = document.getElementById('memory-graph-canvas');
      if (canvas) _drawGraph(canvas);
      _renderGraphCounts();
      _renderGraphWorkbench();
    });
    host.appendChild(chip);
  }
}

function _renderGraphCounts() {
  const el = document.getElementById('memory-graph-counts');
  if (!el) return;
  const total = (value, mounted) => Number.isSafeInteger(value) && value >= mounted ? value : '?';
  const visibleEdges = graphState.tagFilter ? graphState.edges.filter(e => e.tag === graphState.tagFilter).length : graphState.edges.length;
  el.textContent = `showing ${graphState.nodes.length}/${total(graphState.totals.nodes, graphState.nodes.length)} nodes · ${visibleEdges}/${total(graphState.totals.edges, visibleEdges)} edges${graphState.tagFilter ? ` (${graphState.edges.length} mounted)` : ''}`;
}

function _invalidateGraphContext() {
  disposeMemoryGraph({ reason: 'scope-changed' });
  const canvas = graphState.canvas;
  if (canvas && _graphMounted(canvas)) {
    _drawGraph(canvas); _renderGraphCounts(); _renderGraphDetail(null);
  }
}
for (const type of ['workspace-change', 'openclank:auth-context-changed', 'pagehide']) {
  window.addEventListener(type, _invalidateGraphContext);
}
function _retirePairedInspect(event) {
  memoryInspectGeneration++;
  pairedInspect=null; inspectedMemories=inspectedMemories.filter(item=>item.review_kind !== 'assistant_interpretation');
  if (event.type !== 'pagehide') renderMemoryInspect();
}
for (const type of ['workspace-change','openclank:auth-context-changed','pagehide']) window.addEventListener(type, _retirePairedInspect);
document.addEventListener('openclank:auth-context-changed', _retirePairedInspect);
document.addEventListener('openclank:auth-context-changed', _invalidateGraphContext);
// The existing modal owner controls class/hidden and can replace the canvas.
// Observe that actual mount; do not require unrelated app.js ownership edits.
if (document.body) new MutationObserver(() => {
  if (graphState.canvas && (graphState.ready || graphState.pending.size) && !_graphMounted(graphState.canvas)) disposeMemoryGraph();
}).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'hidden'] });

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
    const questionUnavailable = data.digest?.question_eligibility === 'unavailable';
    trustedEl.textContent = data.trusted_block || (questionUnavailable
      ? '(Open-question eligibility is unavailable.)'
      : '(nothing endorsed yet — write or pin a memory, or enable the trust toggles)');
    untrustedEl.textContent = data.untrusted_card || (questionUnavailable
      ? '(No other index entries available.)' : '(memory bank is empty)');
    const stamp = document.getElementById('memory-digest-stamp');
    if (stamp) {
      const at = data.digest?.generated_at;
      stamp.textContent = [at ? `Generated ${at}.` : '', questionUnavailable ? 'Open-question eligibility is unavailable.' : ''].filter(Boolean).join(' ');
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
    trustedEl.textContent = 'Digest unavailable — is the Memery provider running?';
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

// One plain line for a record that supersedes an earlier one, with Show (when the earlier memory still resolves) and Detach.
function _supersedesLine(relation) {
  return relation.target_available === true ? 'Supersedes an earlier memory' : 'Supersedes an earlier memory; the earlier memory is no longer available';
}

function _supersedesRow(memory, relation) {
  const holder = document.createElement('span');
  holder.append(`${_supersedesLine(relation)} `);
  const button = (label, onClick) => {
    const el = document.createElement('button');
    el.type = 'button'; el.textContent = label; el.style.cssText = 'margin-right:6px;font-size:11px;';
    el.addEventListener('click', onClick);
    holder.appendChild(el);
  };
  if (relation.target_available === true && typeof relation.target_id === 'string') {
    button('Show', () => {
      const node = [...document.querySelectorAll('.memory-item[data-memory-id]')].find(n => n.dataset.memoryId === relation.target_id);
      if (node) node.scrollIntoView({ block: 'center' }); else showToast('That memory is not in the current list');
    });
  }
  button('Detach', () => { void _detachMemoryRelation(memory); });
  return _detailRow('Relation', holder);
}

// Detach goes through the same reviewed edit: mirror context -> preview -> confirm -> apply.
async function _detachMemoryRelation(memory) {
  const ticket = _memoryActionTicket();
  try {
    const url = `/api/memory/${encodeURIComponent(memory.id)}`;
    const ctx = await (await fetch(`${url}/mirror-context`, { credentials: 'same-origin' })).json().catch(() => ({}));
    if (ctx.mode !== 'guarded') throw new Error('Memory review context is unavailable');
    const base = { candidate_id: ctx.candidate_id, origin_request_key: ctx.origin_request_key, prior_receipt_id: ctx.prior_receipt_id,
      expected_revision: ctx.expected_revision, request_key: crypto.randomUUID(), reason: 'owner_detach' };
    const post = async (body) => {
      const response = await fetch(`${url}/detach`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(_memoryError(data, 'Detach is unavailable'));
      return data;
    };
    const preview = await post({ phase: 'preview', ...base });
    if (!_memoryActionCurrent(ticket)) return;
    if (!await uiModule.styledConfirm('Detach this memory from the earlier memory it supersedes? Both keep their history.', { title: 'Review memory change', confirmText: 'Detach' })) return;
    if (!_memoryActionCurrent(ticket)) return;
    await post({ phase: 'apply', ...base, operation_json: preview.operation_json, operation_sha256: preview.operation_sha256 });
    await loadMemories();
    showToast('Memory detached');
  } catch (error) {
    showError(error.message || 'Detach is unavailable');
  }
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
  const detailTicket = _memoryActionTicket();
  fetch(`/api/history/memery/${encodeURIComponent(memory.id)}/versions`, {
    credentials: 'same-origin',
  }).then(async response => {
    const detail = await response.json().catch(() => ({}));
    if (!_memoryActionCurrent(detailTicket) || !drawer.isConnected) return;
    history.replaceChildren();
    if (!response.ok) {
      history.appendChild(_detailRow('History', _memoryError(detail, 'Unavailable')));
      return;
    }
    history.appendChild(_detailRow(
      'Current',
      `revision ${detail.current_revision} · ${detail.current?.status || 'unknown'}`,
    ));
    if (detail.supersedes && typeof detail.supersedes === 'object') history.appendChild(_supersedesRow(memory, detail.supersedes));
    else if (detail.current?.revision) {
      const choices = memories.filter(item => item.id && item.id !== memory.id && typeof item.text === 'string' && item.text.trim())
        .slice(0,100).map(item => ({id:item.id,label:item.text.trim().slice(0,100)}));
      history.append(memoryRelationReview({id:memory.id,choices,
        isCurrent:()=>drawer.isConnected && _memoryActionCurrent(detailTicket),onSettled:()=>loadMemories()}));
    }
    for (const revision of (detail.history || []).slice(0, 10)) {
      history.appendChild(_detailRow(
        `Revision ${revision.revision}`,
        [revision.status, revision.created_at, revision.history_reference === 'retained' ? 'Recorded in History' : '', revision.text || '(no value)'].filter(Boolean).join(' · '),
      ));
      if (revision.history_reference === 'unpublished') {
        const checkpoint = document.createElement('button');
        checkpoint.type = 'button';
        checkpoint.className = 'memory-toolbar-btn';
        checkpoint.textContent = 'Save checkpoint';
        checkpoint.addEventListener('click', async () => {
          checkpoint.disabled = true;
          try {
            const response = await fetch(`/api/history/memery/${encodeURIComponent(memory.id)}/versions/${revision.revision}/checkpoint`, {method: 'POST', credentials: 'same-origin'});
            if (!response.ok) throw new Error(_memoryError(await response.json(), 'Checkpoint unavailable'));
            if (_memoryActionCurrent(detailTicket)) checkpoint.textContent = 'Checkpoint saved';
          } catch (error) {
            if (_memoryActionCurrent(detailTicket)) { showError(error.message); checkpoint.disabled = false; }
          }
        });
        history.appendChild(checkpoint);
      }
      const mirrorValue = detail.id !== detail.block_id && revision.value?.type === 'string' &&
        detail.current?.value?.type === 'string' && revision.value.value !== detail.current.value.value;
      if (revision.revision !== detail.current_revision && (mirrorValue || (!revision.evidence_ids?.length &&
          !detail.current?.evidence_ids?.length && !revision.source_event_id && !detail.current?.source_event_id &&
          revision.kind !== 'open_question' && detail.current?.kind !== 'open_question'))) {
        const restore = document.createElement('button');
        restore.type = 'button';
        restore.className = 'memory-toolbar-btn';
        restore.textContent = mirrorValue ? `Restore text from revision ${revision.revision}` : `Restore revision ${revision.revision}`;
        restore.addEventListener('click', async () => {
          restore.disabled = true;
          try {
            if (mirrorValue) {
              await saveInlineEdit(detail.id, revision.value.value, null, {
                expectedRevision: detail.current_revision, reason: `restore text from revision ${revision.revision}`,
                currentMemory: memory, draftCurrent: () => drawer.isConnected && _memoryActionCurrent(detailTicket),
              });
              return;
            }
            const result = await _runVersionedAction(detail, 'revert', {target_revision: revision.revision});
            _publishedActionToast(result, `Restored revision ${revision.revision}`);
          } catch (error) { showError(error.message); }
          finally { if (drawer.isConnected) restore.disabled = false; }
        });
        history.appendChild(restore);
      }
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
      const usesMemery = ['memery', 'frankenmemory'].includes(memoryProviderStatus);
      memoryList.innerHTML = `<div class="memory-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;">
        <span>${usesMemery ? 'No curated memories yet' : 'No memories yet'}${_smiley}</span>
        ${usesMemery ? '<span style="opacity:0.7;font-size:11px;display:block;">Memery keeps raw evidence separate. <a href="#" data-mem-goto-inspect style="color:var(--accent,var(--red));text-decoration:underline;">Review it in Inspect</a>.</span>' : ''}
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

    // The marker only offers a read; the server verifies the current receipt,
    // account and retained Upload before returning any saved analysis.
    let snapshotCandidate = false;
    try {
      const data = JSON.parse(memory.raw_text || memory.text || '');
      snapshotCandidate = data && data.version === 'openclank.analysis-snapshot-memory.v1';
    } catch (_) { /* ordinary memory */ }
    if (snapshotCandidate && !selectMode) {
      const read = document.createElement('button');
      read.type = 'button'; read.className = 'memory-toolbar-btn'; read.textContent = 'Read saved analysis';
      const result = document.createElement('pre');
      result.hidden = true;
      read.addEventListener('click', async () => {
        const ticket = _memoryActionTicket();
        read.disabled = true;
        try {
          const response = await fetch(`/api/memory/${encodeURIComponent(memory.id)}/analysis-snapshot`, { credentials:'same-origin' });
          const data = await response.json();
          if (!_memoryActionCurrent(ticket) || !item.isConnected) return;
          if (!response.ok || data.state !== 'ready') throw new Error('Saved analysis unavailable.');
          result.textContent = `${data.capture_origin === 'imported_unverified' ? 'Imported; source verification unavailable' : 'Captured snapshot'}\n${JSON.stringify(data.result, null, 2)}`;
          for (const component of data.components || []) {
            const recover = document.createElement('button');
            recover.type = 'button';
            recover.className = 'memory-toolbar-btn';
            recover.textContent = `Recover ${component.path.join('/')}`;
            recover.addEventListener('click', async () => {
              recover.disabled = true;
              try {
                const response = await fetch(`/api/history/memery/${encodeURIComponent(memory.id)}/components/${encodeURIComponent(component.resource_id)}`, {credentials: 'same-origin'});
                if (!response.ok) throw new Error(_memoryError(await response.json(), 'Component unavailable'));
                const blob = await response.blob();
                if (!_memoryActionCurrent(ticket) || !item.isConnected) return;
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = component.path[component.path.length - 1];
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              } catch (error) { if (_memoryActionCurrent(ticket)) showError(error.message); }
              finally { if (item.isConnected) recover.disabled = false; }
            });
            result.append(document.createElement('br'), recover);
          }
          result.hidden = false;
        } catch (_) {
          if (_memoryActionCurrent(ticket) && item.isConnected) { result.textContent = 'Saved analysis unavailable.'; result.hidden = false; }
        } finally { if (_memoryActionCurrent(ticket) && item.isConnected) read.disabled = false; }
      });
      const compare = document.createElement('button');
      compare.type = 'button'; compare.className = 'memory-toolbar-btn'; compare.textContent = 'Check original files';
      compare.addEventListener('click', async () => {
        const ticket = _memoryActionTicket(); compare.disabled = true;
        try {
          const [{runSavedSnapshotCompare},{filesFacadeClient}] = await Promise.all([
            import('./analysisNoteFlow.js'), import('./filesFacadeClient.js')]);
          if (!_memoryActionCurrent(ticket) || !item.isConnected) return;
          await runSavedSnapshotCompare({client:filesFacadeClient,memoryID:String(memory.id),scopeRoot:item,
            current:() => _memoryActionCurrent(ticket) && item.isConnected,
            status:message => { if (_memoryActionCurrent(ticket) && item.isConnected) { result.textContent=String(message); result.hidden=false; } }});
        } catch (_) {
          if (_memoryActionCurrent(ticket) && item.isConnected) { result.textContent='Comparison unavailable.'; result.hidden=false; }
        } finally { if (_memoryActionCurrent(ticket) && item.isConnected) compare.disabled=false; }
      });
      content.append(read, compare, result);
    }

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

      bindCardActionMenu(item, {
        button: menuBtn,
        exclude: '.memory-select-cb, .memory-details-drawer',
        current: () => !selectMode,
        items: valid => {
          const ticket = _memoryActionTicket();
          const scope = _pairedScope();
          const items = [
            { id: 'snapshot', label: 'Export knowledge snapshot', dialog: true, run: () =>
              openKnowledgeSnapshotExport({ curatedID: String(memory.id),
                current: () => valid() && _memoryActionCurrent(ticket) && _pairedCurrent(scope) }) },
            { id: 'pin', label: memory.pinned ? 'Unpin' : 'Pin', icon: memory.pinned ? 'bookmark-filled' : 'bookmark', run: () =>
              togglePin(memory.id, !memory.pinned) },
            { id: 'select', label: 'Select', icon: 'select', run: () => {
              if (!selectMode) enterSelectMode();
              selectedIds.add(memory.id);
              updateBulkCount();
              renderMemoryList();
            } },
            { id: 'edit', label: 'Edit', icon: 'edit', run: () => startInlineEdit(item, memory) },
            { id: 'details', label: 'Details', run: () => {
              const existing = item.querySelector('.memory-details-drawer');
              if (existing) { existing.remove(); return; }
              const drawer = _buildMemoryDetails(memory);
              item.appendChild(drawer);
              void _attachS02PolicyEditor(drawer, memory);
              if (item.isConnected && item.getClientRects().length) void recordPresentation('memory.record.opened', {
                recordId: String(memory.id), authorized: true,
              }, { workspaceId: memory.workspace_id || null });
            } },
          ];
          if ((memory.kind === 'unknown' || memory.kind === 'open_question' || memory.category === 'unknown') && !memory.archived) {
            items.push({ id: 'resolve', label: 'Resolve', icon: 'check', dialog: true, run: () => resolveQuestion(memory.id, valid) });
          }
          items.push({ id: 'delete', label: 'Delete', icon: 'trash', destructive: true, dialog: true, run: () => deleteMemory(memory.id, valid) });
          return items;
        },
      });
      item.appendChild(menuBtn);
    }

    memoryList.appendChild(item);
  });

}

// ---- Inline edit with category picker ----

// S02 is an optional, explicit human review; unavailable profiles leave the editor unchanged.
async function _attachS02PolicyEditor(container, memory) {
  const ticket=_memoryActionTicket(); const account=document.body?.dataset?.accountId;
  if (!memory?.id || !account) return;
  const post=async body=>{const r=await fetch('/api/memory/policy/s02',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('Policy is unavailable or changed.');return r.json();};
  try {
    const config=await post({action:'config'});
    if (!_memoryActionCurrent(ticket) || !container.isConnected || document.body?.dataset?.accountId!==account) return;
    const target=await post({action:'targets',curated_id:String(memory.id)}).catch(()=>null);
    if (!_memoryActionCurrent(ticket) || !container.isConnected || document.body?.dataset?.accountId!==account) return;
    if (config.state!=='ready' || !Array.isArray(config.entities) || !Array.isArray(config.definitions)) throw new Error('Policy unavailable for this memory.');
    const selected=target?.state==='ready' && target.item?.selected?.curated_id===String(memory.id) ? target.item : null;
    const box=document.createElement('fieldset'),legend=document.createElement('legend'),status=document.createElement('p');legend.textContent='Type and tag policies';box.append(legend,status);
    const entity=document.createElement('select'),definition=document.createElement('select'),parent=document.createElement('select'),kind=document.createElement('select'),label=document.createElement('input');
    function option(select,text,value){const o=document.createElement('option');o.textContent=text;o.value=value;select.append(o);}
    entity.setAttribute('aria-label','Definition subject');definition.setAttribute('aria-label','Definition');parent.setAttribute('aria-label','Parent definition');kind.setAttribute('aria-label','Definition kind');label.setAttribute('aria-label','New definition name');label.placeholder='New definition name';
    config.entities.forEach((x,i)=>option(entity,x.label,String(i)));config.definitions.forEach((x,i)=>{const n=x.label?.value || x.id;option(definition,n,String(i));option(parent,n,String(i));});option(kind,'Type','type');option(kind,'Tag','tag');
    const axis=document.createElement('select'),mode=document.createElement('select'),value=document.createElement('input');axis.setAttribute('aria-label','Policy axis');mode.setAttribute('aria-label','Policy mode');value.setAttribute('aria-label','Policy value');value.placeholder='0–1 or true/false';['importance','connections','consolidation'].forEach(x=>option(axis,x,x));['rules','model','hybrid'].forEach(x=>option(mode,x,x));
    box.append(entity,label,kind,definition,parent,axis,mode,value);const buttons=document.createElement('div');box.append(buttons);
    const key=`openclank.s02.pending:${account}:${memory.id}`;let busy=false;
    const check=()=>_memoryActionCurrent(ticket)&&container.isConnected&&document.body?.dataset?.accountId===account;
    const receipt=async()=>{const saved=localStorage.getItem(key);if(!saved)return;const pending=JSON.parse(saved);if(pending.kind==='importance_preview'){status.textContent='The preview outcome is unknown and its original is unavailable; it will not be retried.';return;}const r=await post({action:(pending.kind==='importance'||pending.kind==='importance_prepared')?'importance_status':pending.kind==='override'?'override_status':'status',original:pending.original});if(!check())return;if(r.state==='recorded'){localStorage.removeItem(key);status.textContent='Policy review recorded. Reopen to see current revisions.';}else if(r.state==='terminal_erased'||r.state==='account_purged')status.textContent='The original review is no longer available; it will not be retried.';else status.textContent='The original review is not yet known; it will not be retried.';};
    const button=(name,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=name;b.addEventListener('click',async()=>{if(busy||!check())return;busy=true;try{await fn();}catch(e){if(check())status.textContent=e.message||'Policy unavailable.';}finally{busy=false;}});buttons.append(b);};
    const apply=async(changes,item=null)=>{if(localStorage.getItem(key)){status.textContent='Check the pending original review first.';return;}const prepared=await post({action:'prepare',request_key:crypto.randomUUID(),expected_generation:config.generation,changes,item});if(!check()||prepared.state!=='ready')return;if(localStorage.getItem(key)){status.textContent='Check the pending original review first.';return;}localStorage.setItem(key,JSON.stringify({original:prepared.original,item}));try{const r=await post({action:'apply',original:prepared.original,item});if(!check())return;if(['applied','replayed'].includes(r.state)){localStorage.removeItem(key);status.textContent='Policy review recorded. Reopen to refresh.';}else status.textContent='Review outcome unknown; check its status.';}catch(_){if(check())status.textContent='Review outcome unknown; check its status.';}};
    const current=()=>config.definitions[Number(definition.value)];
    button('Create definition',async()=>{const e=config.entities[Number(entity.value)];if(!e||!label.value.trim())throw new Error('Select a subject and enter a name.');await apply([{operation:'create_definition',local_key:crypto.randomUUID(),subject_entity_id:e.id,subject_revision:e.revision,label:label.value.trim(),definition_kind:kind.value,policies:[]}]);});
    button('Set policy',async()=>{const d=current();if(!d)throw new Error('Select a definition.');const x=axis.value==='importance'?Number(value.value):value.value==='true'?true:value.value==='false'?false:null;if(x===null||axis.value==='importance'&&(!value.value.trim()||!Number.isFinite(x)||x<0||x>1))throw new Error('Enter a valid policy value.');const policies=(d.policies||[]).filter(p=>p.axis!==axis.value);policies.push({axis:axis.value,value:x,mode:mode.value,authority:'owner'});await apply([{operation:'definition',block_id:d.id,expected_revision:d.revision,definition_kind:d.kind,policies}]);});
    button('Inherit axis',async()=>{const d=current();if(!d)throw new Error('Select a definition.');const policies=(d.policies||[]).filter(p=>p.axis!==axis.value);policies.push({axis:axis.value,value:null,mode:null,authority:'owner'});await apply([{operation:'definition',block_id:d.id,expected_revision:d.revision,definition_kind:d.kind,policies}]);});
    button('Add parent',async()=>{const d=current(),p=config.definitions[Number(parent.value)];if(!d||!p||d.id===p.id)throw new Error('Select two different definitions.');await apply([{operation:'edge',edge_id:crypto.randomUUID(),expected_revision:0,child_block_id:d.id,child_revision:d.revision,parent_block_id:p.id,parent_revision:p.revision,active:true}]);});
    button('Assign to this memory',async()=>{const d=current();if(!d||!selected)throw new Error('This memory has no current reviewed selection.');const ref=selected.selected.reference;await apply([{operation:'assignment',assignment_id:crypto.randomUUID(),expected_revision:0,target_block_id:ref.block_id,target_revision:ref.revision,definition_block_id:d.id,definition_revision:d.revision,active:true}],selected);});
    const override=async setting=>{if(!config.overrides_available||!selected)throw new Error('Reviewed per-memory overrides are unavailable.');if(localStorage.getItem(key))throw new Error('Check the pending original review first.');const prepared=await post({action:'override_prepare',request_key:crypto.randomUUID(),axis:axis.value,value:setting,item:selected});if(!check()||prepared.state!=='ready')return;if(localStorage.getItem(key))throw new Error('Check the pending original review first.');localStorage.setItem(key,JSON.stringify({kind:'override',original:prepared.original,item:selected}));try{const r=await post({action:'override_apply',original:prepared.original,item:selected});if(!check())return;if(['applied','already_applied'].includes(r.state)){localStorage.removeItem(key);status.textContent='Override reviewed. Reopen this memory to refresh its revision.';}else status.textContent='Review outcome unknown; check its status.';}catch(_){if(check())status.textContent='Review outcome unknown; check its status.';}};
    if(config.overrides_available){button('Set memory override',async()=>{const x=axis.value==='importance'?Number(value.value):value.value==='true'?true:value.value==='false'?false:null;if(x===null||axis.value==='importance'&&(!value.value.trim()||!Number.isFinite(x)||x<0||x>1))throw new Error('Enter a valid value.');await override(x);});button('Inherit for this memory',async()=>override(null));}
    button('Show importance score',async()=>{if(!selected)throw new Error('This memory’s reviewed selection is unavailable.');const r=await post({action:'importance_review',item:selected});if(!check())return;status.textContent=r.state==='ready'?`Importance score: ${r.value} (${r.origin}; revision ${r.revision}).`:'Importance score unavailable.';});
    // Score review uses a separate native capability and retains the exact
    // original selector before issue; a lost reply is reconciled, never retried.
    const scoreSection=document.createElement('section'),scoreTitle=document.createElement('h4'),scoreDetail=document.createElement('p');
    scoreTitle.textContent='Review importance score';scoreDetail.setAttribute('role','status');
    const scoreKind=document.createElement('select'),scoreValue=document.createElement('input'),scoreCandidate=document.createElement('input'),scoreReason=document.createElement('input');
    scoreKind.setAttribute('aria-label','Importance score source');option(scoreKind,'Manual score','manual');option(scoreKind,'Inferred proposal','inferred');
    scoreValue.type='number';scoreValue.min='0';scoreValue.max='1';scoreValue.step='any';scoreValue.setAttribute('aria-label','Manual importance score');
    scoreCandidate.setAttribute('aria-label','Inferred proposal selector');scoreCandidate.placeholder='Proposal selector';scoreReason.setAttribute('aria-label','Importance review reason');scoreReason.placeholder='Reason for review';
    const scorePreview=document.createElement('button'),scoreApply=document.createElement('button');scorePreview.type=scoreApply.type='button';scorePreview.textContent='Preview importance change';scoreApply.textContent='Apply reviewed importance';
    // The Host projects the current Native Once and target capability together.
    const scoreAvailable=config.importance_available===true && !!selected;for(const node of [scoreKind,scoreValue,scoreCandidate,scoreReason,scorePreview])node.disabled=!scoreAvailable;scoreApply.disabled=true;
    scoreDetail.textContent=scoreAvailable?`Target memory ${selected.selected.curated_id}. Choose a manual value or an existing inferred proposal, then preview.`:'Importance score changes are unavailable for this profile.';
    scoreSection.append(scoreTitle,scoreKind,scoreValue,scoreCandidate,scoreReason,scorePreview,scoreApply,scoreDetail);box.append(scoreSection);
    let scoreReview=null;const scoreSignature=()=>JSON.stringify([scoreKind.value,scoreValue.value,scoreCandidate.value,scoreReason.value]);
    const invalidateScore=()=>{scoreReview=null;scoreApply.disabled=true;};
    for(const node of [scoreKind,scoreValue,scoreCandidate,scoreReason])node.addEventListener('input',invalidateScore);
    scoreKind.addEventListener('change',()=>{invalidateScore();scoreValue.hidden=scoreKind.value!=='manual';scoreCandidate.hidden=scoreKind.value!=='inferred';});scoreCandidate.hidden=true;
    try{const saved=JSON.parse(localStorage.getItem(key)||'null');if(saved?.kind==='importance_preview')scoreDetail.textContent='The preview outcome is unknown and its original is unavailable; it will not be retried.';else if(saved?.kind==='importance_prepared'||saved?.kind==='importance')scoreDetail.textContent='Check the original review status; it will not be issued again.';}catch(_){}
    scorePreview.addEventListener('click',async()=>{
      if(busy || !scoreAvailable || !check())return;invalidateScore();busy=true;
      try{
        const source=scoreKind.value,reason=scoreReason.value.trim(),candidate=scoreCandidate.value.trim(),manual=scoreValue.value.trim();
        if(!reason || new TextEncoder().encode(reason).length>512)throw Error('Enter a reason of at most 512 UTF-8 bytes.');
        if(source==='manual' && (!manual || !Number.isFinite(Number(manual)) || Number(manual)<0 || Number(manual)>1))throw Error('Enter a manual score from 0 to 1.');
        if(source==='inferred' && (!candidate || new TextEncoder().encode(candidate).length>512))throw Error('Enter an inferred proposal selector of at most 512 UTF-8 bytes.');
        const signature=scoreSignature(),target=JSON.stringify(selected);
        const input={action:'importance_preview',item:selected,kind:source,value:source==='manual'?Number(manual):null,proposal_candidate_id:source==='inferred'?candidate:null,reason};
        if(localStorage.getItem(key))throw Error('A previous review is unresolved; do not issue another preview.');
        const requestKey=crypto.randomUUID();
        localStorage.setItem(key,JSON.stringify({kind:'importance_preview',input,request_key:requestKey}));
        const reviewed=await post({...input,request_key:requestKey});
        if(!check() || signature!==scoreSignature() || target!==JSON.stringify(selected))return;
        const validScore=x=>typeof x==='number' && Number.isFinite(x) && x>=0 && x<=1;
        const validOriginal=reviewed?.original && typeof reviewed.original==='object' && !Array.isArray(reviewed.original) && new TextEncoder().encode(JSON.stringify(reviewed.original)).length<=65536;
        if(reviewed?.version===1 && reviewed.state==='unknown' && validOriginal){localStorage.setItem(key,JSON.stringify({kind:'importance_prepared',original:reviewed.original,item:selected,request_key:requestKey}));scoreDetail.textContent='The original review outcome is unknown; check its status. It will not be issued again.';return;}
        if(reviewed?.version!==1 || reviewed.state!=='ready' || !validOriginal || !validScore(reviewed.previous?.value) || !['manual','inferred'].includes(reviewed.previous?.origin) || !Number.isSafeInteger(reviewed.previous?.revision) || reviewed.previous.revision<1 || !validScore(reviewed.proposed?.value) || reviewed.proposed?.origin!==source)throw Error('Importance review is unavailable or changed.');
        localStorage.setItem(key,JSON.stringify({kind:'importance_prepared',original:reviewed.original,item:selected,request_key:requestKey}));
        scoreReview={original:reviewed.original,signature,target,requestKey};scoreDetail.textContent=`Target ${selected.selected.curated_id} · current revision ${reviewed.previous.revision} · ${reviewed.previous.value} (${reviewed.previous.origin}) → ${reviewed.proposed.value} (${reviewed.proposed.origin}). Review this change before applying.`;scoreApply.disabled=false;
      }catch(e){if(check()){let saved;try{saved=JSON.parse(localStorage.getItem(key)||'null');}catch(_){}scoreDetail.textContent=saved?.kind==='importance_preview'?'The preview outcome is unknown and its original is unavailable; it will not be retried.':saved?.kind==='importance_prepared'||saved?.kind==='importance'?'Check the original review status; it will not be issued again.':(e.message||'Importance review unavailable.');}}finally{busy=false;}
    });
    scoreApply.addEventListener('click',async()=>{
      if(busy || !scoreAvailable || !check() || !scoreReview || scoreReview.signature!==scoreSignature() || scoreReview.target!==JSON.stringify(selected))return;
      const saved=localStorage.getItem(key);let pending;try{pending=saved?JSON.parse(saved):null;}catch(_){return;}
      if(!pending || pending.kind!=='importance_prepared' || pending.request_key!==scoreReview.requestKey || JSON.stringify(pending.item)!==scoreReview.target || JSON.stringify(pending.original)!==JSON.stringify(scoreReview.original)){scoreDetail.textContent='The original preview is unavailable; do not apply.';return;}
      const review=scoreReview;invalidateScore();busy=true;
      try{
        localStorage.setItem(key,JSON.stringify({kind:'importance',original:review.original,item:selected}));
        const outcome=await post({action:'importance_apply',item:selected,original:review.original});
        if(!check())return;
        if(outcome?.version===1 && ['applied','already_applied'].includes(outcome.state)){localStorage.removeItem(key);scoreDetail.textContent='Importance review applied. Reopen this memory for its current revision.';}
        else scoreDetail.textContent='Importance outcome unknown; check the original review status. Do not apply again.';
      }catch(_){if(check())scoreDetail.textContent='Importance outcome unknown; check the original review status. Do not apply again.';}finally{busy=false;}
    });

    button('Explain this memory',async()=>{if(!selected)throw new Error('This memory has no current reviewed selection.');const r=await post({action:'explain',item:selected});if(!check())return;status.textContent=r.state==='resolved'?Object.entries(r.axes).map(([k,v])=>`${k}: ${v.state==='unavailable'?'manual authority unavailable':v.state}${r.manual_authority?.[k]?.state==='inherit'?' (reviewed inherit)':''}`).join(' · '):'Policy explanation unavailable.';});
    button('Check review status',receipt);if(!selected)status.textContent='This memory’s reviewed selection is unavailable.';else if(config.entities_partial)status.textContent='Some subjects are unavailable.';if(localStorage.getItem(key))status.textContent='An original review is pending; check its status.';container.append(box);
  } catch (_) { if (_memoryActionCurrent(ticket) && container.isConnected && document.body?.dataset?.accountId===account) container.appendChild(_detailRow('Type and tag policies', 'Unavailable for this memory.')); }
}

// Optional metadata has its own exact reviewed revision and must never borrow
// the ordinary text editor's pending request or silently rebase it.
async function _attachMemoryMetadataEditor(container, memory) {
  if (!memory?.id) return;
  const ticket = _memoryActionTicket();
  try {
    const [response, domainResponse] = await Promise.all([
      fetch(`/api/memory/metadata/knowledge/${encodeURIComponent(memory.id)}`, { credentials: 'same-origin' }),
      fetch('/api/memory/metadata/domains', { credentials: 'same-origin' }),
    ]);
    if (!_memoryActionCurrent(ticket) || !container.isConnected || !response.ok || !domainResponse.ok) return;
    const current = await response.json(), domains = await domainResponse.json();
    if (!_memoryActionCurrent(ticket) || !container.isConnected || current.state !== 'ready' || domains.state !== 'ready' || !Array.isArray(domains.items)) return;
    const fieldset = document.createElement('fieldset');
    const legend = document.createElement('legend'); legend.textContent = 'Name and domains'; fieldset.append(legend);
    const name = document.createElement('input'); name.type = 'text'; name.placeholder = 'Name (optional)'; name.value = current.metadata?.name || ''; name.setAttribute('aria-label', 'Memory name');
    const aliases = document.createElement('textarea'); aliases.rows = 2; aliases.placeholder = 'Aliases, one per line'; aliases.value = (current.metadata?.aliases || []).join('\n'); aliases.setAttribute('aria-label', 'Memory aliases');
    fieldset.append(name, aliases);
    const selected = new Set((current.metadata?.domain_refs || []).map(ref => `${ref.block_id}:${ref.revision}`));
    const checks = [];
    for (const domain of domains.items) {
      const label = document.createElement('label'), checkbox = document.createElement('input'); checkbox.type = 'checkbox';
      checkbox.checked = selected.has(`${domain.block_id}:${domain.revision}`); label.append(checkbox, document.createTextNode(domain.name)); fieldset.append(label);
      checks.push({ checkbox, block_id: domain.block_id, revision: domain.revision });
    }
    const save = document.createElement('button'); save.type = 'button'; save.textContent = 'Save name and domains';
    let issued = false;
    save.addEventListener('click', async () => {
      if (issued || !_memoryActionCurrent(ticket)) return;
      if (current.metadata?.name && !name.value.trim()) { showError('A saved name cannot be cleared here. Enter a name to continue.'); return; }
      const metadata = { aliases: aliases.value.split('\n').map(v => v.trim()).filter(Boolean), domain_refs: checks.filter(v => v.checkbox.checked).map(({block_id, revision}) => ({block_id, revision})) };
      if (name.value.trim()) metadata.name = name.value.trim();
      issued = true; save.disabled = true;
      try {
        const result = await fetch(`/api/memory/metadata/knowledge/${encodeURIComponent(memory.id)}`, { method: 'PUT', credentials: 'same-origin', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ revision: current.revision, predecessor_receipt_id: current.predecessor_receipt_id, request_key: crypto.randomUUID(), metadata }) });
        if (!_memoryActionCurrent(ticket) || !container.isConnected) return;
        const value = await result.json().catch(() => ({}));
        if (!_memoryActionCurrent(ticket) || !container.isConnected) return;
        if (!result.ok) throw new Error(_memoryError(value, 'Metadata may not have been saved. Reopen to check.'));
        showToast('Name and domains saved'); fieldset.remove();
      } catch (error) { if (_memoryActionCurrent(ticket)) showError(error.message); }
    });
    fieldset.append(save);
    const create = document.createElement('details'), createLabel = document.createElement('summary'); createLabel.textContent = 'Create a domain'; create.append(createLabel);
    const domainName = document.createElement('input'); domainName.placeholder = 'Domain name'; domainName.setAttribute('aria-label','New domain name');
    const definition = document.createElement('textarea'); definition.placeholder = 'Definition'; definition.setAttribute('aria-label','New domain definition');
    const createButton = document.createElement('button'); createButton.type = 'button'; createButton.textContent = 'Create reviewed domain';
    let createIssued = false;
    createButton.addEventListener('click', async () => {
      if (createIssued || !_memoryActionCurrent(ticket) || !domainName.value.trim() || !definition.value.trim()) return;
      createIssued = true; createButton.disabled = true;
      try {
        const response = await fetch('/api/memory/metadata/domains', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_key:crypto.randomUUID(),name:domainName.value.trim(),aliases:[],value:definition.value.trim()})});
        if (!_memoryActionCurrent(ticket) || !container.isConnected) return;
        const value=await response.json().catch(()=>({}));
        if (!_memoryActionCurrent(ticket) || !container.isConnected) return;
        if (!response.ok) throw new Error(_memoryError(value,'Domain may not have been created. Reopen to check.'));
        showToast('Domain created. Reopen this editor to assign it.');create.remove();
      } catch (error) {if (_memoryActionCurrent(ticket)) showError(error.message);}
    });
    create.append(domainName,definition,createButton);fieldset.append(create);container.append(fieldset);
  } catch (_) { /* Optional profile or transport is unavailable. */ }
}

async function _attachStandingGuidance(container, memory) {
  const ticket = _memoryActionTicket();
  const endpoint = `/api/memory/${encodeURIComponent(memory.id)}/standing-guidance/`;
  const post = async (phase, body) => {
    const response = await fetch(endpoint + phase, {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result = await response.json().catch(() => ({}));
    if (!_memoryActionCurrent(ticket) || !container.isConnected) throw new Error('Review is no longer current.');
    if (!response.ok) throw new Error('Standing guidance is unavailable or the outcome is unknown.');
    return result;
  };
  try {
    const current = await post('read',{decision:'approve'});
    if (!['ready','unavailable'].includes(current.state) || typeof current.scope !== 'string') return;
    const key = `openclank.standing-guidance.v1:${current.scope}:${memory.id}`;
    const box=document.createElement('fieldset'), legend=document.createElement('legend');
    legend.textContent='Standing guidance';box.append(legend);
    const status=document.createElement('p');status.textContent=current.decision==='approve' ? 'Approved for chats in this Memory workspace.' : 'Not currently approved for standing guidance.';box.append(status);
    if (blocksPublicLinks(current)) {const blocks=document.createElement('p');blocks.textContent='Blocks public file links';box.append(blocks);}
    const blockLabel=document.createElement('label'), blockBox=document.createElement('input');blockBox.type='checkbox';blockBox.checked=false;
    blockLabel.append(blockBox,document.createTextNode(' Also block public file links while this guidance is current'));box.append(blockLabel);
    const check=document.createElement('button');check.type='button';check.textContent='Check review status';
    const approve=document.createElement('button');approve.type='button';approve.textContent='Review approval';
    const withdraw=document.createElement('button');withdraw.type='button';withdraw.textContent='Review withdrawal';
    const pending=()=>localStorage.getItem(key);
    const sync=()=>{const has=!!pending();check.hidden=!has;approve.disabled=has || current.state!=='ready';withdraw.disabled=has || current.state!=='ready';};
    check.addEventListener('click',async()=>{try {
      const raw=pending();if (!raw)return;const selector=JSON.parse(raw);
      if (selector.scope!==current.scope || selector.curated_id!==String(memory.id)) throw new Error('Recovery selector is unavailable.');
      const result=await post('status',selector);
      if (result.state!=='applied') {status.textContent='Outcome unknown. Check status again; no review was resent.';return;}
      if (pending()===raw)localStorage.removeItem(key);sync();
      status.textContent=result.current ? `Review ${result.decision === 'approve' ? 'approved' : 'withdrawn'}.` : 'The review was recorded, but a later revision makes it inactive.';
    } catch (error) {if (_memoryActionCurrent(ticket)) status.textContent=error.message;} });
    const review=async decision=>{try {
      if (pending())return;
      const blockPublic=decision==='approve' && blockBox.checked;
      const preview=await post('preview',withRequirement({decision},blockPublic));
      if (preview.state!=='preview' || typeof preview.value!=='string' || typeof preview.reviewer!=='string' || !preview.selector || preview.selector.scope!==current.scope || preview.selector.curated_id!==String(memory.id)) throw new Error('Review unavailable.');
      const text=`${decision==='approve'?'Approve':'Withdraw'} standing guidance as ${preview.reviewer} for chats in this Memory workspace?\n\n${preview.value}\n\nEditing this value or its revision invalidates this approval.`;
      if (!window.confirm(text) || !_memoryActionCurrent(ticket) || !container.isConnected)return;
      const raw=JSON.stringify(blockPublic ? withRequirement(preview.selector,true) : preview.selector);localStorage.setItem(key,raw);
      if (localStorage.getItem(key)!==raw)throw new Error('Recovery could not be saved; nothing was sent.');
      sync();status.textContent='Review submitted; check status if the result is unknown.';
      const result=await post('apply',withRequirement({decision,selector:preview.selector,confirmed:true},blockPublic));
      if (result.state!=='applied')throw new Error('Outcome unknown. Check status; do not send again.');
      if (pending()===raw)localStorage.removeItem(key);sync();status.textContent=`Review ${decision==='approve'?'approved':'withdrawn'}.`;
    } catch(error) {if (_memoryActionCurrent(ticket)) {status.textContent=error.message;sync();}} };
    approve.addEventListener('click',()=>void review('approve'));withdraw.addEventListener('click',()=>void review('withdraw'));
    box.append(approve,withdraw,check);container.append(box);sync();
  } catch (_) { /* Unsupported, unqualified, or inaccessible memory. */ }
}

async function _attachKeepPreparedChoice(container, memory, editState) {
  const ticket = _memoryActionTicket();
  try {
    const response = await fetch(`/api/memory/${encodeURIComponent(memory.id)}/mirror-context`, { credentials: 'same-origin' });
    const context = await response.json();
    if (!response.ok || typeof context.keep_prepared_scope !== 'string' || !_memoryActionCurrent(ticket) || !container.isConnected) return;
    const storeKey = `openclank.prepared-recovery.v1:${context.keep_prepared_scope}:${memory.id}`;
    const raw = localStorage.getItem(storeKey);
    if (raw !== null) {
      const selector = JSON.parse(raw);
      if (!selector || selector.version !== 1 || selector.scope !== context.keep_prepared_scope || selector.curated_id !== String(memory.id) || Object.keys(selector).sort().join(',') !== 'block_id,curated_id,expected_revision,predecessor_receipt_id,request_sha256,scope,version') throw new Error('Saved recovery selector is invalid.');
      editState.pending = { keepPrepared:true, issued:true, restored:true, selector, storeKey };
      editState.sync?.();
    }
    if (context.keep_prepared_available !== true) return;
    const label = document.createElement('label');
    const choice = document.createElement('input'); choice.type = 'checkbox'; choice.disabled = !!editState.pending;
    choice.addEventListener('change', () => { if (!editState.pending) editState.keepPrepared = choice.checked; });
    label.append(choice, document.createTextNode(' Keep this edited memory prepared'));
    container.append(label);
  } catch (_) { if (_memoryActionCurrent(ticket) && container.isConnected) showError('Prepared memory recovery is unavailable.'); }
}

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
  const editState = { pending: null, inFlight: false, sync: () => {} };
  const saveDraft = async () => {
    if (editState.inFlight) return;
    editState.inFlight = true; saveBtn.disabled = true;
    const text = input.value, category = catSelect.value;
    try { return await saveInlineEdit(memory.id, text, category, { currentMemory: memory, editState,
      draftCurrent: () => input.isConnected && input.value === text && catSelect.value === category });
    } finally { editState.inFlight = false; saveBtn.disabled = false; editState.sync(); }
  };
  saveBtn.addEventListener('click', saveDraft);

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'memory-item-btn';
  cancelBtn.textContent = 'cancel';
  cancelBtn.addEventListener('click', () => {
    if (editState.pending) return showError('This request may have completed. Retry it or choose Review again first.');
    renderMemoryList();
  });

  const reviewAgain = document.createElement('button');
  reviewAgain.className = 'memory-item-btn'; reviewAgain.textContent = 'Review again'; reviewAgain.hidden = true;
  reviewAgain.addEventListener('click', async () => {
    if (editState.inFlight || !editState.pending || editState.pending.keepPrepared) return;
    if (await uiModule.styledConfirm('The previous request may have completed. Review the current memory again?', { title: 'Review again', confirmText: 'Review again' })) {
      editState.pending = null; editState.sync();
    }
  });
  editState.sync = () => { saveBtn.textContent = editState.pending ? (editState.pending.keepPrepared ? 'Check status' : 'Retry') : 'save'; reviewAgain.hidden = !editState.pending || editState.pending.keepPrepared; };

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  actions.appendChild(reviewAgain);

  item.appendChild(editRow);
  item.appendChild(actions);
  void _attachMemoryMetadataEditor(item, memory);
  void _attachS02PolicyEditor(item, memory);
  void _attachKeepPreparedChoice(item, memory, editState);
  void _attachStandingGuidance(item, memory);

  input.focus();
  input.select();

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveDraft();
    if (e.key === 'Escape') {
      e.stopPropagation();
      e.stopImmediatePropagation();
      if (editState.pending) showError('This request may have completed. Retry it or choose Review again first.');
      else renderMemoryList();
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
  const editState = { pending: null, inFlight: false, sync: () => {} };
  save.addEventListener('click', async () => {
    if (editState.inFlight) return;
    editState.inFlight = true;
    save.disabled = true;
    const draftText = input.value, draftCategory = category.value;
    const updated = await saveInlineEdit(
      memory.id,
      draftText,
      draftCategory,
      { refreshInspect, refreshDigest, currentMemory: memory, editState,
        draftCurrent: () => input.isConnected && input.value === draftText && category.value === draftCategory },
    );
    editState.inFlight = false;
    if (!updated) save.disabled = false;
    editState.sync();
  });
  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.className = 'memory-item-btn';
  cancelButton.textContent = 'cancel';
  cancelButton.addEventListener('click', () => {
    if (editState.pending) return showError('This request may have completed. Retry it or choose Review again first.');
    cancel?.();
  });
  const reviewAgain = document.createElement('button');
  reviewAgain.type = 'button'; reviewAgain.className = 'memory-item-btn'; reviewAgain.textContent = 'Review again'; reviewAgain.hidden = true;
  reviewAgain.addEventListener('click', async () => {
    if (editState.inFlight || !editState.pending || editState.pending.keepPrepared) return;
    if (await uiModule.styledConfirm('The previous request may have completed. Review the current memory again?', { title: 'Review again', confirmText: 'Review again' })) {
      editState.pending = null; editState.sync();
    }
  });
  editState.sync = () => { save.textContent = editState.pending ? (editState.pending.keepPrepared ? 'Check status' : 'Retry') : 'save'; reviewAgain.hidden = !editState.pending || editState.pending.keepPrepared; };
  actions.append(save, cancelButton, reviewAgain);
  card.append(editRow, actions);
  void _attachMemoryMetadataEditor(card, memory);
  void _attachS02PolicyEditor(card, memory);
  void _attachKeepPreparedChoice(card, memory, editState);
  void _attachStandingGuidance(card, memory);
  input.focus();
  input.select();
}

async function saveInlineEdit(id, newText, newCategory, {
  refreshInspect = false,
  refreshDigest = false,
  currentMemory = null,
  draftCurrent = () => true,
  editState = { pending: null },
  expectedRevision = null,
  reason = 'owner_edit',
} = {}) {
  if (expectedRevision === null) newText = newText.trim();
  if (!newText) return false;

  const memory = memories.find(m => String(m.id) === String(id))
    || inspectedMemories.find(m => String(m.id) === String(id))
    || currentMemory;
  const catChanged = newCategory && newCategory !== (memory?.category || 'fact');
  const storedText = memory ? (memory.raw_text || memory.text) : null;
  if (!editState.pending && (!memory || (newText === storedText && !catChanged))) {
    if (refreshInspect) renderMemoryInspect();
    else if (!refreshDigest) renderMemoryList();
    return true;
  }

  try {
    const ticket = _memoryActionTicket();
    const applyPending = async () => {
      const pending = editState.pending;
      if (!pending || (!pending.restored && (pending.text !== newText || pending.category !== newCategory))) throw new Error('The previous request may have completed. Restore that draft to retry or choose Review again.');
      const reconcile = pending.keepPrepared && pending.issued;
      const body = reconcile ? JSON.stringify(pending.selector) : new URLSearchParams({ mirror_review: pending.sealed });
      if (pending.keepPrepared) pending.issued = true;
      const response = await fetch(reconcile ? `/api/memory/${encodeURIComponent(id)}/mirror-keep-status` : `/api/memory/${encodeURIComponent(id)}`, reconcile
        ? { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body }
        : { method: 'PUT', credentials: 'same-origin', body });
      if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The request may have completed. Your draft and retry are preserved.');
      const data = await response.json().catch(() => ({}));
      if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The request may have completed. Your draft and retry are preserved.');
      if (!response.ok || (reconcile ? data.state !== 'applied' : data.ok !== true || data.review?.state !== 'applied')) throw new Error(pending.keepPrepared ? 'The outcome is unknown. Check status again; the edit will not be sent again.' : 'The request may have completed. Retry it or choose Review again.');
      if (pending.keepPrepared) {
        const saved=localStorage.getItem(pending.storeKey);
        if (saved===JSON.stringify(pending.selector)) localStorage.removeItem(pending.storeKey);
      }
      editState.pending = null; editState.sync?.();
      await loadMemories();
      if (refreshInspect) await loadMemoryInspect();
      if (refreshDigest) await loadDigestPreview();
      showToast('Memory updated');
      return true;
    };
    if (editState.pending) return await applyPending();
    const contextResponse = await fetch(`/api/memory/${encodeURIComponent(id)}/mirror-context`, { credentials: 'same-origin' });
    const context = await contextResponse.json().catch(() => ({}));
    if (!contextResponse.ok) throw new Error(_memoryError(context, 'Memory review context is unavailable'));
    if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The edit changed while review was opening. Your draft is still here.');
    if (expectedRevision !== null && context.mode === 'guarded' && context.expected_revision !== expectedRevision) {
      throw new Error('The current revision changed. Reopen History before restoring.');
    }
    if (context.mode === 'guarded') {
      if (catChanged) throw new Error('Category changes are not available for guarded memories. Your draft is still here.');
      if (newText === storedText) return true;
      const requestKey = crypto.randomUUID();
      const request = { candidate_id: context.candidate_id, origin_request_key: context.origin_request_key,
        prior_receipt_id: context.prior_receipt_id, expected_revision: context.expected_revision,
        request_key: requestKey, reason, selected_paths: ['/value/value'] };
      const keepPrepared = editState.keepPrepared === true && context.keep_prepared_available === true;
      const previewResponse = await fetch(`/api/memory/${encodeURIComponent(id)}/mirror-preview${keepPrepared ? '?keep_prepared=1' : ''}`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...request, payload: { value: { type: 'string', value: newText } } }),
      });
      const preview = await previewResponse.json().catch(() => ({}));
      if (!previewResponse.ok) throw new Error(_memoryError(preview, 'Memory preview is unavailable'));
      if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The edit changed while review was opening. Your draft is still here.');
      if (keepPrepared) {
        if (preview.state !== 'preview' || typeof preview.old_value !== 'string' || typeof preview.operation_json !== 'string' || typeof preview.operation_sha256 !== 'string' || !preview.policy_projection) throw new Error('Prepared memory preview is unavailable.');
        const approved = await uiModule.styledConfirm(`Before:\n${preview.old_value}\n\nAfter:\n${newText}\n\nKeep this edited memory prepared.`, { title: 'Review memory change', confirmText: 'Apply' });
        if (!approved) return false;
        if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The edit changed after preview.');
        const selector=preview.selector;
        if (!selector || typeof selector.scope !== 'string' || selector.curated_id !== String(id) || typeof preview.request_key !== 'string') throw new Error('Prepared memory recovery is unavailable.');
        const storeKey=`openclank.prepared-recovery.v1:${selector.scope}:${id}`;
        const saved=JSON.stringify(selector);
        if (localStorage.getItem(storeKey)!==null) throw new Error('An earlier prepared edit must be reconciled first.');
        localStorage.setItem(storeKey,saved);
        if (localStorage.getItem(storeKey)!==saved) throw new Error('Prepared memory recovery could not be saved.');
        editState.pending = { text: newText, category: newCategory, keepPrepared: true, issued: false, selector, storeKey,
          sealed: JSON.stringify({ ...request, request_key: preview.request_key, operation_json: preview.operation_json, operation_sha256: preview.operation_sha256, policy_projection: preview.policy_projection, keep_prepared: true }) };
        editState.sync?.();
        return await applyPending();
      }
      const change = Array.isArray(preview.changes) ? preview.changes.find(c => c.path === '/value/value') : null;
      if (!change || !change.before?.present || !change.after?.present || typeof change.before.value !== 'string' || typeof change.after.value !== 'string' || change.after.value !== newText) throw new Error('Memory preview is unavailable. Your draft is still here.');
      const approved = await uiModule.styledConfirm(`Before:\n${change.before.value}\n\nAfter:\n${change.after.value}`, { title: 'Review memory change', confirmText: 'Apply' });
      if (!approved) return false;
      if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The edit changed after preview. Your draft is still here.');
      editState.pending = { text: newText, category: newCategory, sealed: JSON.stringify({ ...request,
        operation_json: preview.operation_json, operation_sha256: preview.operation_sha256 }) };
      editState.sync?.();
      return await applyPending();
    }
    if (context.mode !== 'legacy') throw new Error('Memory review context is unavailable');
    const params = new URLSearchParams({ text: newText });
    if (newCategory) params.append('category', newCategory);

    const response = await fetch(`${window.location.origin}/api/memory/${id}`, {
      method: 'PUT',
      credentials: 'same-origin',
      body: params
    });

    if (!_memoryActionCurrent(ticket) || !draftCurrent()) throw new Error('The edit context changed. Your draft is still here.');

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

const _originalMemoryAdds = new Map();
export async function addNewMemory() {
  const input = document.getElementById('new-memory-input');
  const text = input.value.trim();
  const category = _readNewMemoryCategory();
  const ticket = _memoryActionTicket();
  const account = document.body?.dataset?.accountId;
  if (!text) { showError('Memory text cannot be empty'); return; }
  if (!account) { showError('Account is unavailable.'); return; }
  const old = _originalMemoryAdds.get(account);
  if (old && (old.text !== text || old.category !== category)) {
    showError('Check the original Add outcome before changing its text or category.'); return;
  }
  const original = old || {account, text, category, request_key: crypto.randomUUID()};
  if (original.busy) return;
  original.busy = true;
  _originalMemoryAdds.set(account, original);
  try {
    const response = await fetch(`${window.location.origin}/api/memory/add`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'same-origin',
      body: JSON.stringify({text: original.text, category: original.category,
                            request_key: original.request_key, action: old ? 'status' : 'submit'})
    });
    if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      if (body?.reason === 'memory_preparing') throw new Error('Memory is still being prepared for this account. Try again in a moment.');
      throw new Error('Original Add outcome unavailable; select Add again to check status.');
    }
    const result = await response.json();
    if (!_memoryActionCurrent(ticket) || document.body?.dataset?.accountId !== account) return;
    if (result.state === 'absent') { showError('Original Add was not found; its outcome remains unresolved.'); return; }
    const guarded = ['applied','staged','terminal_erased'].includes(result.state);
    const legacy = !old && result.ok === true && !Object.hasOwn(result, 'state')
      && (typeof result.memory_id === 'string' || (Number.isSafeInteger(result.count) && result.message === 'Memory already exists'));
    if (!guarded && !legacy) throw new Error('Original Add outcome unavailable.');
    _originalMemoryAdds.delete(account);
    if (input.value.trim() === original.text && _readNewMemoryCategory() === original.category) input.value = '';
    await loadMemories();
    if (_memoryActionCurrent(ticket)) showToast(result.state === 'terminal_erased' ? 'Original Add was erased' : result.pending_review ? 'Sent to Candidates for review' : 'Memory added');
  } catch (error) {
    if (_memoryActionCurrent(ticket)) showError(error.message || 'Original Add outcome unavailable; check status.');
  } finally { original.busy = false; }
}

export async function editMemory(id) {
  const memory = memories.find(m => m.id === id);
  if (!memory) return;
  const item = [...document.querySelectorAll('.memory-item[data-memory-id]')].find(node => node.dataset.memoryId === String(id));
  if (item) startInlineEdit(item, memory);
  else showError('Open this memory in the list to edit it.');
}

async function resolveQuestion(id, valid = () => true) {
  const ticket = _memoryActionTicket();
  if (!valid() || !_memoryActionCurrent(ticket)) return;
  const answer = prompt('Answer this open question:');
  if (!answer?.trim() || !valid() || !_memoryActionCurrent(ticket)) return;
  try {
    let expectedRevision = '';
    const history = await fetch(`/api/memory/${encodeURIComponent(id)}/history`, {
      credentials: 'same-origin',
    });
    if (!valid() || !_memoryActionCurrent(ticket)) return;
    let review = null;
    if (history.ok) {
      const detail = await history.json();
      if (!valid() || !_memoryActionCurrent(ticket)) return;
      expectedRevision = String(detail.current_revision || '');
      if (Number.isSafeInteger(detail.current_revision) && detail.current_revision > 0 &&
          [detail.block_id, detail.subject_entity_id, detail.predicate, detail.claim_slot].every(value => typeof value === 'string' && value.trim())) {
        const question = detail.current?.text || 'this question';
        const subject = detail.entity?.canonical_label || detail.subject_entity_id;
        if (!confirm(`Answer “${question}” for ${subject} (${detail.predicate} / ${detail.claim_slot}) with “${answer.trim()}”?`)) return;
        review = detail;
      }
    }
    const form = new URLSearchParams({ answer: answer.trim() });
    if (expectedRevision) form.set('expected_revision', expectedRevision);
    if (review) {
      form.set('question_block_id', review.block_id);
      form.set('subject_entity_id', review.subject_entity_id);
      form.set('predicate', review.predicate);
      form.set('claim_slot', review.claim_slot);
      form.set('request_key', crypto.randomUUID());
      form.set('selected_paths', JSON.stringify(['/status', '/value/type', '/value/value']));
    }
    if (!valid() || !_memoryActionCurrent(ticket)) return;
    const res = await fetch(`${window.location.origin}/api/memory/${encodeURIComponent(id)}/resolve`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    });
    if (!valid() || !_memoryActionCurrent(ticket)) return;
    if (res.ok) {
      await loadMemories({ isCurrent: () => _memoryActionCurrent(ticket) });
      if (_memoryActionCurrent(ticket)) showToast('Question answered in the same memory');
    } else {
      const err = await res.json().catch(() => ({}));
      if (_memoryActionCurrent(ticket)) showError(_memoryError(err, 'Failed to resolve question'));
    }
  } catch (e) {
    if (!valid() || !_memoryActionCurrent(ticket)) return;
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

export async function deleteMemory(id, valid = () => true) {
  const code = graphState.nodes.find(node => node.id === id && _isCodeGraphNode(node));
  const memory = memories.find(m => m.id === id) || (code ? { ...code, text: code.label || code.name } : null);
  if (!memory) return;
  const action = _memoryActionTicket();

  if (!await uiModule.styledConfirm(`Delete this memory?\n"${memory.text}"`, { confirmText: 'Delete', danger: true })) return;
  if (!valid() || !_memoryActionCurrent(action)) return;
  disposeMemoryGraph({ clear: false, reason: 'forget' });
  action.loadId = graphState.loadId;

  try {
    const response = await fetch(`${window.location.origin}/api/memory/${id}`, {
      method: 'DELETE'
    });
    if (!_memoryActionCurrent(action)) return;

    if (response.ok) {
      _removeForgottenGraphNodes([id]);
      await animateMemoryRemoval([id]);
      if (!_memoryActionCurrent(action)) return;
      await loadMemories({ isCurrent: () => _memoryActionCurrent(action) });
      if (!_memoryActionCurrent(action)) return;
      showToast('Memory deleted');
    } else {
      const failure = await _memoryDeleteFailure(response);
      if (_memoryActionCurrent(action)) showError(failure);
    }
  } catch (error) {
    if (!_memoryActionCurrent(action)) return;
    showError('Failed to delete memory');
  }
}

function _memoryActionTicket() {
  const modal = document.getElementById('memory-modal');
  return { context: _graphContext(), loadId: graphState.loadId, modal,
    visible: Boolean(modal && !modal.closest('.hidden, [hidden]')) };
}

function _isCodeGraphNode(node) {
  // Typed producer/projection provenance only; never classify every file or
  // infer code authority from a display name/checkout-looking prefix.
  return node?.kind === 'code_symbol' || node?.domain === 'code' || node?.provenance?.domain === 'code';
}

function _memoryActionCurrent(ticket) {
  return ticket.context === _graphContext() && ticket.loadId === graphState.loadId
    && (!ticket.visible || (ticket.modal?.isConnected && !ticket.modal.closest('.hidden, [hidden]')));
}

async function _memoryDeleteFailure(response) {
  const { problem } = await responseError(response, 'Failed to delete memory');
  if (problem?.code === 'code_node_forget_requires_workspace_erase') {
    // Known public contract, never interpolate raw native/private diagnostics.
    return 'A code-graph node cannot be forgotten individually. Use explicit workspace-index erasure to remove its indexed code. Nothing was deleted.';
  }
  if (problem?.code === 'f1_edited_head') {
    return 'This updated memory cannot be forgotten individually yet. Nothing was deleted.';
  }
  if (problem?.code === 'memory_delete_unsupported') {
    return 'This memory cannot be deleted in this form yet, so nothing was deleted.';
  }
  return 'Failed to delete memory';
}

function _removeForgottenGraphNodes(ids) {
  if (!ids.length) return;
  const removed = new Set(ids);
  graphState.nodes = graphState.nodes.filter(node => !removed.has(node.id));
  graphState.edges = graphState.edges.filter(edge => !removed.has(edge.src_id) && !removed.has(edge.dst_id));
  if (removed.has(graphState.selected)) graphState.selected = null;
  graphState.totals = { nodes: null, edges: null };
  if (_graphMounted(graphState.canvas)) {
    _relayoutGraph(graphState.canvas); _renderGraphTags(); _renderGraphCounts();
    _renderGraphDetail(graphState.selected);
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

function _memoryImportCurrent(intent) {
  return !!intent && _pairedCurrent(intent.scope)
    && document.body?.dataset?.accountId === intent.account
    && (sessionModule?.getCurrentSessionId?.() || null) === intent.sessionId;
}

async function _configureMemoryRoute(problem, route, file, intent) {
  if (!_memoryImportCurrent(intent)) throw new Error('The original import account or session changed.');
  const files = Array.isArray(file) ? file : [file];
  const routeId = route?.model_route_id;
  if (!routeId) throw new Error('The selected Memory model route is no longer available.');
  showToast(`Configuring ${route.display_name || 'the selected model'} for Memory…`, {
    duration: 15000,
    leadingIcon: 'spinner',
  });
  if (!_memoryImportCurrent(intent)) throw new Error('The original import account or session changed.');
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
  if (!_memoryImportCurrent(intent)) throw new Error('The original import account or session changed.');
  if (!response.ok) {
    const failure = await responseError(response, 'Memory model setup failed');
    if (!_memoryImportCurrent(intent)) throw new Error('The original import account or session changed.');
    throw new Error(failure.message);
  }
  if (pendingMemoryImportIntent === intent) {
    pendingMemoryImportFile = null;
    pendingMemoryImportFiles = [];
    pendingMemoryImportIntent = null;
  }
  document.dispatchEvent(new CustomEvent('open-clank:providers-updated'));
  showToast(`${route.display_name || 'Model'} is now the Memory model. Retrying import…`, {
    duration: 5000,
    leadingIcon: 'check',
  });
  await handleImportFiles(files, intent);
}

async function _openMemoryRouteSettings(problem, file, intent) {
  if (!_memoryImportCurrent(intent)) throw new Error('The original import account or session changed.');
  const files = Array.isArray(file) ? file : [file];
  pendingMemoryImportFiles = files.filter(Boolean);
  pendingMemoryImportFile = pendingMemoryImportFiles[0] || null;
  pendingMemoryImportIntent = intent;
  const settingsModule = await import('./settings.js');
  if (!_memoryImportCurrent(intent)) return;
  settingsModule.open(problem.settings_target || 'added-models');
  showToast('Choose and save a Memory model route; this file will retry automatically.', {
    duration: 12000,
  });
}

function _showMemoryRouteProblem(problem, file, detail, intent) {
  intent ||= {actionKey: crypto.randomUUID(), account: document.body?.dataset?.accountId, scope: _pairedScope(), sessionId: sessionModule?.getCurrentSessionId?.() || null};
  const files = Array.isArray(file) ? file : [file];
  pendingMemoryImportFiles = files.filter(Boolean);
  pendingMemoryImportFile = pendingMemoryImportFiles[0] || null;
  pendingMemoryImportIntent = intent;
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
      ? _configureMemoryRoute(problem, only, file, intent)
      : _openMemoryRouteSettings(problem, file, intent),
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

async function _waitForImportBatch(batchId, { timeoutMs = 300000, intervalMs = 3000, isCurrent = () => true } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline && isCurrent()) {
    try {
      const response = await fetch(
        `/api/memory/import-batches/${encodeURIComponent(batchId)}`,
        { credentials: 'same-origin' },
      );
      if (response.ok) {
        last = await response.json();
        if (!isCurrent()) return null;
        if (String(last?.state || '') !== 'active') return last;
      }
    } catch (error) {
      console.warn('Import batch status poll failed:', error);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return last;
}

async function _openImportBatchReview(batchId, isCurrent = () => true) {
  const data = await _waitForImportBatch(batchId, {isCurrent});
  if (!data || !isCurrent()) return false;
  _renderBatchReview(data);
  _refreshPendingImportNotice(isCurrent);
  return true;
}

async function _recoverImportBatchAfterTimeout(isCurrent = () => true) {
  if (!isCurrent()) return false;
  showToast('Import is still processing — watching for it to finish…');
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline && isCurrent()) {
    const batches = await _fetchPendingImportBatches();
    if (!isCurrent()) return false;
    // Only recover a batch the server touched recently; a stale pending
    // batch from an earlier session is resumed via the notice, not here.
    const latest = batches.find((batch) => {
      const updated = Date.parse(String(batch?.updated_at || ''));
      return batch?.batch_id && Number.isFinite(updated) && Date.now() - updated < 15 * 60 * 1000;
    });
    if (latest) {
      return _openImportBatchReview(latest.batch_id, isCurrent);
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  return false;
}

async function _refreshPendingImportNotice(isCurrent = () => true) {
  if (!isCurrent()) return;
  const notice = document.getElementById('memory-import-pending');
  if (!notice) return;
  const batches = await _fetchPendingImportBatches();
  if (!isCurrent()) return;
  const actionable = batches.find((batch) => String(batch?.state) === 'active')
    || batches.find((batch) => _importBatchReviewPending(batch))
    || batches.find((batch) => Number(batch?.counts?.failed || 0) > 0);
  notice.classList.remove('hidden');
  notice.replaceChildren();
  const snapshotStatus = document.createElement('button');
  snapshotStatus.type='button'; snapshotStatus.className='memory-item-btn'; snapshotStatus.textContent='Check snapshot save';
  snapshotStatus.addEventListener('click', async () => {
    const ticket=_memoryActionTicket(); snapshotStatus.disabled=true;
    try {
      const {runSnapshotPublicationStatus}=await import('./analysisNoteFlow.js');
      if (!_memoryActionCurrent(ticket) || !notice.isConnected) return;
      const result=await runSnapshotPublicationStatus({scopeRoot:notice,current:() => _memoryActionCurrent(ticket) && notice.isConnected,
        status:message => { if (_memoryActionCurrent(ticket)) showToast(String(message)); }});
      if (_memoryActionCurrent(ticket) && result?.state==='applied') await loadMemories();
    } catch (_) { if (_memoryActionCurrent(ticket)) showError('Snapshot status unavailable.'); }
    finally { if (_memoryActionCurrent(ticket) && notice.isConnected) snapshotStatus.disabled=false; }
  });
  notice.appendChild(snapshotStatus);
  if (!actionable) return;
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
  const failed = Number(actionable.counts?.failed || 0);
  label.textContent = pending
    ? `${pending} imported suggestion${pending === 1 ? '' : 's'} waiting for review${files}`
    : `${failed} import file${failed === 1 ? '' : 's'} need attention${files}`;
  const review = document.createElement('button');
  review.className = 'memory-item-btn save';
  review.textContent = pending ? 'review' : 'check import status';
  review.addEventListener('click', () => {
    const ticket = _memoryActionTicket();
    _openImportBatchReview(actionable.batch_id, () => _memoryActionCurrent(ticket));
  });
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
        text: String(reviewState === 'publishing' && typeof review?.proposal?.text === 'string' ? review.proposal.text : text),
        category: (reviewState === 'publishing' && review?.proposal?.category) || (typeof suggestion === 'object' && suggestion?.category) || 'fact',
        filename: item.filename || 'upload',
        itemId: item.item_id || null,
        questionContext: (reviewState === 'publishing' ? review?.proposal?.question_context : (typeof suggestion === 'object' && suggestion?.question_context)) || null,
        review,
        reviewState,
        markdownSource: typeof suggestion === 'object' ? suggestion?.markdown_source : null,
        snapshotImport: typeof suggestion === 'object' && suggestion?.snapshot_import?.capture_origin === 'imported_unverified',
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
  if (item.reviewState === 'publishing') throw new Error('Check the original review status.');
  const key = _batchReviewIdempotencyKey(item);
  if (action === 'accept') {
    item.reviewState = 'publishing';
    item.originalProposal = {...proposal};
  }
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
  if (item.reviewState === 'awaiting_review' && item.review?.last_failure?.code === 'IMPORT_REVIEW_NOT_APPLIED') {
    if (!result?.batch || typeof result.batch !== 'object') throw new Error('Original review outcome is unavailable.');
    return {...result, originalNotApplied:true};
  }
  return result;
}

function _appendBatchItemError(card, message) {
  const existing = card.querySelector('.memory-import-error');
  const errorText = existing || document.createElement('small');
  errorText.textContent = message;
  errorText.className = 'memory-import-error';
  if (!existing) card.appendChild(errorText);
}

function _batchPublishingControls(card, item, controls, fields, updateTitle) {
  for (const field of fields) if (field) field.disabled = true;
  controls.replaceChildren();
  const notice = document.createElement('small');
  notice.textContent = 'Original review outcome is unknown; check its status.';
  const check = document.createElement('button'); check.type = 'button'; check.className = 'memory-item-btn'; check.textContent = 'Check status';
  check.addEventListener('click', async () => {
    const scope = _pairedScope();
    if (!scope) return showError('The original account is unavailable.');
    check.disabled = true;
    try {
      const response = await fetch(`${window.location.origin}/api/memory/import-batches/${encodeURIComponent(item.batchId)}/review`, {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({phase:'status',suggestion_id:item.suggestionId})});
      const result = await response.json().catch(() => ({}));
      if (!_pairedCurrent(scope) || !card.isConnected) return;
      if (!response.ok || result.ok !== true || !result.batch || typeof result.batch !== 'object')
        throw new Error('Original review outcome is unavailable.');
      _renderBatchReview(result.batch);
      const review = result.review?.review || result.review;
      if (review?.state === 'awaiting_review' && review?.last_failure?.code === 'IMPORT_REVIEW_NOT_APPLIED')
        showError('The original review was not applied. Review it again.');
      updateTitle();
    } catch (error) { if (_pairedCurrent(scope) && card.isConnected) _appendBatchItemError(card,error.message || 'Original review outcome is unavailable.'); }
    finally { if (_pairedCurrent(scope) && card.isConnected) check.disabled = false; }
  });
  controls.append(notice,check);
}

function _batchFailedItems(data) {
  return (Array.isArray(data?.items) ? data.items : [])
    .filter((item) => item?.state === 'failed_terminal')
    .map((item) => ({
      batchId: String(data?.batch_id || ''),
      itemId: String(item?.item_id || ''),
      filename: String(item?.filename || 'upload'),
      retryable: item?.error?.retryable !== false && !item?.superseded_by,
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
  const markdownItems = (Array.isArray(data?.items) ? data.items : [])
    .filter((item) => item?.result?.markdown_bundle?.state === 'unverified' && !item?.superseded_by);
  body.innerHTML = '';
  body.classList.remove('hidden');
  if (memList) memList.classList.add('hidden');
  if (reviewItems.length === 0 && failedItems.length === 0 && markdownItems.length === 0) {
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
      if (!item.active || !item.text || item.snapshotImport || item.reviewState === 'publishing') continue;
      try {
        const result = await _submitBatchReview(item, 'accept');
        if (result.originalNotApplied) { _renderBatchReview(result.batch); showError('The original review was not applied. Review it again.'); return; }
        saved += 1;
      } catch (error) {
        item.error = error?.message || 'Failed to save memory';
        if (item.reviewState === 'publishing' && item.statusOnly) item.statusOnly();
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

  for (const item of markdownItems) {
    const bundle = item.result.markdown_bundle;
    const source = document.createElement('p');
    source.className = 'memory-import-source';
    const origin = bundle.foreign ? 'foreign origin claim' : 'matching origin label';
    const omissions = bundle.omitted_items
      ? `. Export reports ${bundle.omitted_items}${bundle.omissions_at_least ? '+' : ''} omitted items`
      : '';
    source.textContent = `${item.filename || 'Markdown upload'} · verified upload checksum · ${origin}, unverified${omissions}.`;
    body.appendChild(source);
  }

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
    if (item.snapshotImport) {
      const card = document.createElement('div'); card.className = 'memory-suggestion-item';
      const label = document.createElement('div');
      label.textContent = `Imported snapshot; source verification unavailable. An existing copy of the same snapshot with different wording conflicts. ${item.text}`;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-item-btn save';
      const imported = {batch_id:String(item.batchId),item_id:String(item.itemId),suggestion_id:String(item.suggestionId)};
      const key = `openclank.snapshot-review.v1:${item.batchId}:${item.itemId}:${item.suggestionId}`;
      let selector = null;
      try { const raw = localStorage.getItem(key); if (raw) selector = JSON.parse(raw); } catch (_) { /* unavailable below */ }
      button.textContent = selector ? 'Check status' : 'Remember snapshot';
      button.addEventListener('click', async () => {
        const ticket = _memoryActionTicket(); button.disabled = true;
        try {
          const post = async payload => {
            const response = await fetch('/api/memory/analysis-snapshot/publication', {method:'POST', credentials:'same-origin',
              headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            const result = await response.json();
            if (!response.ok) throw new Error('Snapshot outcome is unavailable. Check status.');
            return result;
          };
          if (!selector) {
            const prepared = await post({phase:'prepare',imported});
            if (!_memoryActionCurrent(ticket) || !card.isConnected) return;
            if (prepared.state !== 'prepared' || !prepared.selector) throw new Error('Snapshot review unavailable.');
            const raw=JSON.stringify(prepared.selector); localStorage.setItem(key,raw);
            if (localStorage.getItem(key)!==raw) throw new Error('Recovery storage unavailable; snapshot was not sent.');
            selector=prepared.selector;
            button.textContent='Check status';
            const result=await post({phase:'issue',imported,selector});
            if (!_memoryActionCurrent(ticket) || !card.isConnected) return;
            if (result.state!=='applied') throw new Error('Snapshot outcome is unknown. Check status.');
          } else {
            const result=await post({phase:'status',imported,selector});
            if (!_memoryActionCurrent(ticket) || !card.isConnected) return;
            if (result.state!=='applied') throw new Error('Snapshot outcome is unknown. Check status.');
          }
          localStorage.removeItem(key); item.active=false; card.remove(); updateTitle(); await loadMemories();
        } catch (error) {
          if (_memoryActionCurrent(ticket) && card.isConnected) _appendBatchItemError(card,error?.message || 'Snapshot unavailable.');
        } finally { if (_memoryActionCurrent(ticket) && card.isConnected) button.disabled=false; }
      });
      card.append(label,button); body.appendChild(card); continue;
    }
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
    content.appendChild(source);
    if (item.markdownSource?.state === 'unverified') {
      const claim = item.markdownSource;
      const sourceDetails = document.createElement('small');
      sourceDetails.className = 'memory-import-source';
      const details = [];
      if (claim.source_sha256) details.push(`declared checksum ${claim.source_sha256}`);
      if (Number.isSafeInteger(claim.source_revision)) details.push(`declared revision ${claim.source_revision}`);
      if (claim.revision_omitted) details.push('declared revision outside supported range');
      if (claim.source_uri_sha256 || claim.source_messages_sha256) details.push('declared locators retained as fingerprints');
      sourceDetails.textContent = details.length ? `Unverified source · ${details.join(' · ')}` : 'Source attribution unavailable.';
      content.appendChild(sourceDetails);
    }
    content.append(text, category);
    if (about) content.appendChild(about);
    const controls = document.createElement('div');
    controls.className = 'memory-suggestion-actions';
    item.statusOnly = () => _batchPublishingControls(card, item, controls, [text,category,about], updateTitle);
    const save = document.createElement('button');
    save.className = 'memory-item-btn save';
    save.textContent = 'save';
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const result = await _submitBatchReview(item, 'accept');
        if (result.originalNotApplied) { _renderBatchReview(result.batch); showError('The original review was not applied. Review it again.'); return; }
        card.remove();
        updateTitle();
        await loadMemories();
        const outcome = result?.review?.review?.state || result?.review?.state;
        showToast(outcome === 'reused' ? 'Already represented in Memory; kept this source receipt' : 'Saved to memory');
      } catch (error) {
        item.error = error?.message || 'Failed to save memory';
        if (item.reviewState === 'publishing') item.statusOnly();
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
    if (item.reviewState === 'publishing') item.statusOnly();
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
    card.appendChild(content);
    body.appendChild(card);
    if (!item.retryable) continue;
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
    card.appendChild(controls);
  }
  modal.classList.remove('hidden');
}

async function handleImportFiles(files, originalIntent = null) {
  const selected = Array.from(files || []).filter(Boolean);
  if (!selected.length) return;
  // Every interactive import uses the durable batch/review authority, even
  // for one text file. The legacy route remains an API compatibility shim,
  // but its client-side /add flow cannot preserve server-owned provenance or
  // proposed entity matches.
  const account = document.body?.dataset?.accountId;
  if (!account) { showError('Account is unavailable.'); return; }
  const intent = originalIntent || {actionKey: crypto.randomUUID(), account, scope: _pairedScope(), sessionId: sessionModule?.getCurrentSessionId?.() || null};
  const isCurrent = () => _memoryImportCurrent(intent);
  if (!isCurrent()) { showError('The original import account changed.'); return; }
  const sessionId = intent.sessionId;
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
    // Bind a possible explicit historical-metadata choice to the same bytes
    // that were examined by the first request.
    const uploadFiles = selected.length === 1
      ? [new File([await selected[0].arrayBuffer()], selected[0].name, { type: selected[0].type })]
      : selected;
    if (!isCurrent()) return;
    const formData = new FormData();
    for (const file of uploadFiles) formData.append('files', file);
    if (sessionId) formData.append('session', sessionId);
    formData.append('action_key', intent.actionKey);
    if (!isCurrent()) return;
    const response = await fetch(`${window.location.origin}/api/memory/import-batches`, {
      method: 'POST',
      body: formData,
    });
    if (!isCurrent()) return;
    if (!response.ok) {
      const failure = await responseError(response, 'Import failed');
      if (!isCurrent()) return;
      if (failure.problem?.code === 'HISTORICAL_OBSERVATIONS_REQUIRE_SINGLE_IMPORT' && uploadFiles.length === 1) {
        if (!isCurrent()) return;
        const approved = window.confirm('Retain this file’s historical metadata as unverified? This will not create memory observations, learning, or model extraction. Other memory content and media in the file will not be imported.');
        if (!approved || !isCurrent()) return;
        const historical = new FormData();
        historical.append('file', uploadFiles[0]);
        historical.append('retain_unverified_history', 'true');
        if (sessionId) historical.append('session', sessionId);
        if (!isCurrent()) return;
        const retained = await fetch(`${window.location.origin}/api/memory/import`, { method: 'POST', body: historical });
        if (!isCurrent()) return;
        if (!retained.ok) throw new Error((await responseError(retained, 'Historical metadata retention failed')).message);
        const result = await retained.json();
        if (!isCurrent()) return;
        if (result?.historical_observations?.state !== 'external_unverified') throw new Error('Historical metadata retention was not confirmed.');
        pendingMemoryImportFiles = []; pendingMemoryImportFile = null;
        if (pendingMemoryImportIntent === intent) pendingMemoryImportIntent = null;
        showToast(result.message || 'Unverified historical metadata retained.');
        return;
      }
      const error = new Error(failure.message);
      error.problem = failure.problem;
      throw error;
    }
    pendingMemoryImportFiles = [];
    pendingMemoryImportFile = null;
    if (pendingMemoryImportIntent === intent) pendingMemoryImportIntent = null;
    const data = await response.json();
    if (!isCurrent()) return;
    _renderBatchReview(data);
  } catch (error) {
    if (!isCurrent()) return;
    console.error('Batch import failed:', error);
    if (error?.problem?.code === 'MEMORY_ROUTE_UNCONFIGURED') {
      _showMemoryRouteProblem(error.problem, selected, error.message, intent);
    } else {
      // A proxy read timeout can sever the response while the durable batch
      // keeps processing server-side; recover it instead of lying "failed".
      // Structured server rejections (413/400 problems) never created a
      // batch, so only unstructured failures are worth recovering.
      const recovered = error?.problem ? false : await _recoverImportBatchAfterTimeout(isCurrent);
      if (!isCurrent()) return;
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
    if (data.snapshot_review) {
      pendingMemoryImportFile = null;
      _renderBatchReview(data.snapshot_review);
      return;
    }
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

function _wireMemorySourceSearch() {
  const input = document.getElementById('memory-search');
  if (!input) return;

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'memory-toolbar-btn';
  button.textContent = 'Search journal titles';
  const workspaceButton = document.createElement('button');
  workspaceButton.type = 'button';
  workspaceButton.className = 'memory-toolbar-btn';
  workspaceButton.textContent = 'Search workspace file contents';
  const results = document.createElement('div');
  results.hidden = true;
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const entries = document.createElement('ul');
  results.append(status, entries);
  input.after(button, workspaceButton, results);

  let active = null;
  const clear = () => {
    active?.abort();
    active = null;
    entries.replaceChildren();
    status.textContent = '';
    results.hidden = true;
  };
  input.addEventListener('input', clear);
  for (const event of ['openclank:auth-context-changed', 'workspace-change', 'pagehide'])
    window.addEventListener(event, clear);
  document.addEventListener('openclank:auth-context-changed', clear);
  document.querySelectorAll('.memory-tab[data-memory-tab]:not([data-memory-tab="browse"])')
    .forEach(tab => tab.addEventListener('click', clear));

  const activeFileWorkspace = () => sessionModule.isCurrentSessionIncognito?.() ? '' : Storage.get(KEYS.WORKSPACE_ID, '') || '';
  const renderMatch = (label, href, expectedRevision, current, handoffCurrent, sourceLine) => {
    const item = document.createElement('li');
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'memory-item-btn';
    open.textContent = label;
    open.addEventListener('click', async () => {
      if (!current()) return;
      open.disabled = true;
      try {
        const copal = await import('./copal.js');
        if (!current()) return;
        const opened = await copal.openResource(href, { expectedRevision, sourceLine, isCurrent:current, handoffCurrent });
        if (current() && !opened) status.textContent = 'The selected source could not be opened.';
      } catch (_) {
        if (current()) status.textContent = 'The source changed or is unavailable. Search again for its current revision.';
      } finally {
        open.disabled = false;
      }
    });
    item.append(open);
    entries.append(item);
  };

  workspaceButton.addEventListener('click', async () => {
    clear();
    results.hidden = false;
    const query = input.value.trim();
    const account = _pairedScope();
    const workspace = activeFileWorkspace();
    if (!account || !workspace) {
      status.textContent = 'Choose a workspace to search its authorized files.';
      return;
    }
    if (!query || query.length > 256 || /[\r\n\u0000]/.test(query)) {
      status.textContent = 'Enter up to 256 characters on one line.';
      return;
    }

    const controller = new AbortController();
    active = controller;
    const handoffCurrent = () => active === controller && _pairedCurrent(account)
      && activeFileWorkspace() === workspace && input.value.trim() === query;
    const current = () => handoffCurrent() && results.isConnected && !results.closest('.hidden, [hidden]');
    status.textContent = 'Searching workspace file contents…';
    try {
      const { resolveWorkspaceId } = await import('./workspace.js');
      if (!current()) return;
      const selected = await resolveWorkspaceId(workspace, 'app_folder');
      if (!current()) return;
      if (selected.id !== workspace || !Number.isSafeInteger(selected.revision) || selected.revision < 1) throw new Error();
      const response = await fetch('/api/files-v1/workspace-content-search', {
        method:'POST', credentials:'same-origin', cache:'no-store', signal:controller.signal,
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({workspace_id:workspace, expected_revision:selected.revision, query}),
      });
      const page = await response.json();
      if (!current()) return;
      if (!response.ok || page?.version !== 1 || page.workspace_id !== workspace
          || page.workspace_revision !== selected.revision || !['complete', 'partial'].includes(page.state)
          || !Array.isArray(page.hits) || page.hits.length > 12) throw new Error();

      status.textContent = page.state === 'partial'
        ? `Bounded search returned ${page.hits.length} exact file matches. Some files or matches could not be included.`
        : page.hits.length ? `${page.hits.length} exact file matches.` : 'No matches in the eligible workspace files.';
      for (const hit of page.hits) {
        renderMatch(`${hit.label} · line ${hit.line}`, hit.ref, hit.revision, current, handoffCurrent, hit.line);
      }
    } catch (_) {
      if (current()) status.textContent = 'Workspace source search is unavailable. Check the selected workspace and its Files access.';
    }
  });

  const selectedWorkspace = () => window.__odysseusGetActiveCopalContext?.()?.workspace || 'default';
  button.addEventListener('click', async () => {
    clear();
    const query = input.value.trim();
    const account = _pairedScope();
    const workspace = selectedWorkspace();
    results.hidden = false;
    if (!account || !query || query.length > 256) {
      status.textContent = account ? 'Enter a title search of 1 to 256 characters.' : 'Sign in to search your Journal.';
      return;
    }

    const controller = new AbortController();
    const { signal } = controller;
    active = controller;
    const handoffCurrent = () => active === controller && _pairedCurrent(account)
      && selectedWorkspace() === workspace && input.value.trim() === query;
    const current = () => handoffCurrent() && results.isConnected && !results.closest('.hidden, [hidden]');
    status.textContent = 'Searching active Journal titles…';
    try {
      const roots = await filesFacadeClient.roots({ copalWorkspace:workspace, signal });
      if (!current()) return;
      const root = roots.entries?.find(entry => entry.provider === 'copal' && entry.kind === 'provider_root'
        && entry.provenance?.workspace === workspace);
      if (!root?.ref) throw new Error('Journal is unavailable');
      const folders = await filesFacadeClient.children(root.ref, { limit:8, signal });
      if (!current()) return;
      const journal = folders.entries?.find(entry => entry.provider === 'copal'
        && entry.provenance?.view === 'active' && entry.provenance?.corpus === 'notes');
      if (!journal?.ref) throw new Error('Journal folder is unavailable');
      const page = await filesFacadeClient.children(journal.ref, { query, limit:12, signal });
      if (!current()) return;
      const matches = (page.entries || []).filter(entry => entry.provider === 'copal' && entry.kind === 'document'
        && entry.ref && entry.revision?.kind === 'copalHead' && entry.revision.value
        && entry.capabilities?.includes('open'));
      status.textContent = matches.length ? `${matches.length} Journal title matches${page.next_cursor ? ' on this page' : ''}.`
        : 'No revision-qualified matches on this page.';
      for (const entry of matches) {
        renderMatch(entry.name, entry.ref, entry.revision, current, handoffCurrent);
      }
    } catch (_) {
      if (current()) status.textContent = 'Journal is unavailable in the current workspace.';
    }
  });
}

// Event listeners
document.addEventListener('DOMContentLoaded', () => {
  _wireMemorySourceSearch();
  void initMemoryRankLearning();
  document.getElementById('memory-consolidate-now')?.addEventListener('click',async event=>{
    const scope=_pairedScope();const label=document.getElementById('memory-consolidate-status');
    const chat=Storage.get(KEYS.CURRENT_SESSION,'');
    if (!scope || !chat) {label.textContent='Open a chat first.';return;}
    const button=event.currentTarget;button.disabled=true;label.textContent='Checking this chat…';
    try {
      const response=await fetch('/api/memory/consolidate-now',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:chat})});
      const result=await response.json();
      if (!_pairedCurrent(scope)) return;
      label.textContent = response.ok
        ? `Consolidation status: ${String(result.state || 'unavailable').replaceAll('_', ' ')}.`
        : 'Consolidation is unavailable.';
      const skipReasons = {
        route_unavailable: 'The selected model route is unavailable.',
        source_changed: 'A source changed.',
        gates_closed: 'Idle or work-budget admission is unavailable.',
        run_unavailable: 'The admitted work did not settle in this pass.',
      };
      if (response.ok && Object.hasOwn(skipReasons, result.skip_reason)) label.textContent += ' ' + skipReasons[result.skip_reason];
      const coverage = result.coverage;
      if (response.ok && coverage && ['included', 'already_covered', 'omitted', 'superseded', 'gap']
          .every((key) => Number.isSafeInteger(coverage[key]) && coverage[key] >= 0)) {
        label.textContent += ` Page coverage: ${coverage.included} eligible, ${coverage.already_covered} already covered, ${coverage.omitted} omitted, ${coverage.superseded} superseded, ${coverage.gap} gaps.`;
      }
      void loadRetainedConsolidationRun();
    } catch (_) {if (_pairedCurrent(scope)) label.textContent='Consolidation is unavailable.';}
    finally {button.disabled=false;}
  });
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
      if (target === 'explorer') import('./memoryExplorer.js').then(m => m.loadExplorer(document.getElementById('memory-explorer-root'), null, {
        getScope: _pairedScope, getSession: () => sessionModule?.getCurrentSessionId?.() || null,
      }));
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
  if (bulkDeleteBtn) { const bulkSnapshotBtn = bulkDeleteBtn.cloneNode(false); bulkSnapshotBtn.id = 'memory-bulk-snapshot'; bulkSnapshotBtn.classList.remove('danger'); bulkSnapshotBtn.disabled = false; bulkSnapshotBtn.textContent = 'Export selected as snapshots…';
    bulkSnapshotBtn.addEventListener('click', () => { const ticket=_memoryActionTicket(), scope=_pairedScope(); openKnowledgeSnapshotBundleExport({curatedIDs:[...selectedIds].map(String),current:()=>bulkSnapshotBtn.isConnected && _memoryActionCurrent(ticket) && _pairedCurrent(scope)}); });
    bulkDeleteBtn.after(bulkSnapshotBtn); }
  if (bulkDeleteBtn) {
    const bulkEditBtn = bulkDeleteBtn.cloneNode(false); bulkEditBtn.id = 'memory-bulk-edit'; bulkEditBtn.classList.remove('danger'); bulkEditBtn.disabled = true; bulkEditBtn.textContent = 'Edit selected…';
    bulkEditBtn.addEventListener('click', () => {
      const ticket = _memoryActionTicket(); const chosen = memories.filter(m => selectedIds.has(m.id));
      if (!chosen.length || !_memoryActionCurrent(ticket)) return;
      void openMemoryUpdateBatch({ memories: chosen, notify: showToast, onDone: async () => { exitSelectMode(); await loadMemories(); } });
    });
    bulkDeleteBtn.before(bulkEditBtn);
  }

  const bulkCancelBtn = document.getElementById('memory-bulk-cancel');
  if (bulkCancelBtn) bulkCancelBtn.addEventListener('click', exitSelectMode);

  const exportBtn = document.getElementById('memory-export-btn');
  if (exportBtn) exportBtn.addEventListener('click', exportMemories);
  if (exportBtn) { const markdownBtn = exportBtn.cloneNode(false); markdownBtn.id = 'memory-export-markdown-btn'; markdownBtn.textContent = 'Export as Markdown'; markdownBtn.title = 'Download your memories as a Markdown file with a versioned manifest (read-only; nothing is changed)';
    markdownBtn.addEventListener('click', () => { const anchor = document.createElement('a'); anchor.href = '/api/memory/export/markdown'; anchor.download = 'openclank-memory.md'; document.body.appendChild(anchor); anchor.click(); anchor.remove(); });
    const filesBtn = exportBtn.cloneNode(false); filesBtn.id = 'memory-export-markdown-files-btn'; filesBtn.textContent = 'Export as Markdown files (zip)'; filesBtn.title = 'Download one Markdown file per memory in a zip (read-only; nothing is changed)';
    filesBtn.addEventListener('click', () => { const anchor = document.createElement('a'); anchor.href = '/api/memory/export/markdown?layout=files'; anchor.download = 'openclank-memory.zip'; document.body.appendChild(anchor); anchor.click(); anchor.remove(); });
    exportBtn.after(markdownBtn); markdownBtn.after(filesBtn); }

  const importBtn = document.getElementById('memory-import-btn');
  if (importBtn) importBtn.addEventListener('click', importMemories);
  if (importBtn) importBtn.title = 'Import memories from a file. A Markdown export from Open Clank is imported exactly and still goes through review.';
  if (importBtn) { const snapshotImportBtn = importBtn.cloneNode(false); snapshotImportBtn.id = 'memory-import-snapshot-btn'; snapshotImportBtn.textContent = 'Import snapshot…'; snapshotImportBtn.title = 'Import one exported knowledge snapshot as a review-only draft';
    snapshotImportBtn.addEventListener('click', () => { const ticket=_memoryActionTicket(), scope=_pairedScope(); openKnowledgeSnapshotImport({current:()=>snapshotImportBtn.isConnected && _memoryActionCurrent(ticket) && _pairedCurrent(scope)}); });
    importBtn.after(snapshotImportBtn); }
  if (importBtn) { const bundleImportBtn = importBtn.cloneNode(false); bundleImportBtn.id = 'memory-import-snapshot-bundle-btn'; bundleImportBtn.textContent = 'Import snapshot bundle…'; bundleImportBtn.title = 'Import several exported knowledge snapshots as review-only drafts';
    bundleImportBtn.addEventListener('click', () => { const ticket=_memoryActionTicket(), scope=_pairedScope(); openKnowledgeSnapshotBundleImport({current:()=>bundleImportBtn.isConnected && _memoryActionCurrent(ticket) && _pairedCurrent(scope)}); });
    document.getElementById('memory-import-snapshot-btn').after(bundleImportBtn); }

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
    const intent = pendingMemoryImportIntent;
    if (!intent) { memoryImportRetrying = false; return; }
    handleImportFiles(files, intent).finally(() => { memoryImportRetrying = false; });
  }, 0);
});

async function openMemoryReference(id) {
  if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,255}$/.test(id)) return;
  const scope = _pairedScope(), session = sessionModule?.getCurrentSessionId?.() || null;
  if (!scope) return;
  const modal = document.getElementById('memory-modal');
  const root = document.getElementById('memory-explorer-root');
  if (!modal || !root) return;
  modal.classList.remove('hidden');
  document.querySelectorAll('.memory-tab').forEach(t => t.classList.toggle('active', t.dataset.memoryTab === 'explorer'));
  document.querySelectorAll('.memory-tab-panel[data-memory-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.memoryPanel !== 'explorer'));
  const module = await import('./memoryExplorer.js');
  if (!_pairedCurrent(scope) || (sessionModule?.getCurrentSessionId?.() || null) !== session) return;
  await module.loadExplorer(root, id, { getScope: _pairedScope, getSession: () => sessionModule?.getCurrentSessionId?.() || null });
}

const memoryModule = {
  mountLearningSettings,
  openReference: openMemoryReference,
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
