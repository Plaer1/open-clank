// Typed owner preferences. The server owns capability, validation and admission.
import { get, KEYS } from './storage.js';

const CORE = [
  ['compaction.auto', 'boolean', 'set-agentCompactionAuto', 'Automatic compaction'],
  ['compaction.prune', 'boolean', 'set-agentCompactionPrune', 'Prune old tool output'],
  ['compaction.tail_turns', 'integer', 'set-agentTailTurns', 'Recent turns', 0, 1000],
  ['compaction.preserve_recent_tokens', 'integer', 'set-agentPreserveRecent', 'Recent tokens', 0, 8000000],
  ['compaction.reserved', 'integer', 'set-agentCompactionReserved', 'Compaction reserve', 0, 2000000],
  ['compaction.max_context', 'quantity', 'set-agentMaxContext', 'Maximum context'],
  ['checkpoint.thresholds', 'string_array', 'set-agentCheckpointThresholds', 'Thresholds'],
  ['checkpoint.reserved', 'integer', 'set-agentCheckpointReserved', 'Checkpoint reserve', 0, 2000000],
  ['checkpoint.max_writer_failures', 'integer', 'set-agentMaxWriterFailures', 'Max writer failures', 1, 100],
  ['checkpoint.fork', 'boolean', 'set-agentCheckpointFork', 'Fork checkpoint writer'],
  ['checkpoint.push_caps', 'object', 'set-agentPushCaps', 'Push caps'],
].map(([path, type, id, label, min, max]) => ({path, type, id, label, min, max, available:path !== 'checkpoint.max_writer_failures', ...(path === 'checkpoint.max_writer_failures' ? {resettable:true, reason:'Saved legacy value only; retries use failure class and context progress (CLASS_PROGRESS_RETRY_OWNER).'} : {}), scope:'account', apply:'next_admission'}));
let catalogFields = CORE;

export function nativeSettingControls() {
  return catalogFields.map(field => ({path:field.path, supported:field.available !== false,
    availability:field.available === false ? 'unavailable' : 'available', writable:field.available !== false,
    referencePaths:Array.isArray(field.reference_paths) ? field.reference_paths : [], scope:field.scope || 'account',
    destination:{panel:field.path.startsWith('tokenomics.') ? 'tokenomics' : 'ai',field:field.path}, reason:field.reason || ''}));
}

export function agentSettingsCatalogEntries() {
  return catalogFields.map(field => ({
    identity: 'setting:' + field.path,
    token: 'settings ' + (field.path.startsWith('tokenomics.') ? 'tokenomics ' : 'ai ') + field.path,
    aliases: [], help: field.available === false && field.resettable === true
      ? 'Reset only: ' + (field.reason || field.help || field.label || field.path)
      : field.help || field.label || field.path,
    category: 'Settings', origin: 'host', executor: 'settings',
    scope: field.scope || 'account', destination: {panel:field.path.startsWith('tokenomics.') ? 'tokenomics' : 'ai', field:field.path},
    availability: field.available === false && field.resettable !== true ? 'unavailable' : 'available',
    underlyingAvailability: field.available === false ? 'unavailable' : 'available',
    writable: field.available !== false, resettable:field.resettable === true, reason:field.reason || '',
  }));
}

function at(value, path) {
  return path.split('.').reduce((node, key) => node && Object.hasOwn(node, key) ? node[key] : undefined, value);
}
function put(value, path, item) {
  const keys = path.split('.'); let node = value;
  for (const key of keys.slice(0,-1)) node = node[key] ||= {};
  node[keys.at(-1)] = item;
}

export function parseAgentField(field, raw) {
  if (raw === '') return undefined;
  if (field.type === 'boolean') {
    if (!['true','false'].includes(raw)) throw new Error(field.path + ': choose inherit, enabled or disabled');
    return raw === 'true';
  }
  if (field.type === 'integer' || field.type === 'number') {
    const number = Number(raw);
    if (!Number.isFinite(number) || (field.type === 'integer' && !Number.isSafeInteger(number)) ||
        (field.min != null && number < field.min) || (field.max != null && number > field.max)) {
      throw new Error(field.path + ': enter a valid ' + field.type + (field.min != null ? ' from ' + field.min : '') + (field.max != null ? ' to ' + field.max : ''));
    }
    return number;
  }
  if (field.type === 'string_array') {
    const value = raw.trim().startsWith('[') ? JSON.parse(raw) : raw.split(',').map(x=>x.trim()).filter(Boolean);
    if (!Array.isArray(value) || value.some(x=>typeof x !== 'string')) throw new Error(field.path + ': expected an array of strings');
    return value;
  }
  if (field.type === 'json') return JSON.parse(raw);
  if (field.type === 'object' || field.type === 'quantity' && raw.trim().startsWith('{')) {
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(field.path + ': expected an object');
    return value;
  }
  if (field.type === 'quantity' && /^\d+(?:\.\d+)?$/.test(raw.trim())) return Number(raw);
  return raw;
}

export function buildAgentSettingsMutation(fields, controls, saved, resets, loadedValues) {
  const payload={compaction:{},checkpoint:{}}, resetPaths=new Set(resets);
  for(const field of fields) {
    const input=controls.get(field.path);if(!input||field.available===false)continue;
    if(field.path.startsWith('distill.') && !resets.has(field.path) && input.value===loadedValues.get(field.path))continue;
    const value=parseAgentField(field,field.type === 'string' ? input.value : input.value.trim());
    if(value===undefined) {if(at(saved,field.path)!==undefined)resetPaths.add(field.path);}
    else {put(payload,field.path,value);resetPaths.delete(field.path);}
  }
  return {agent_settings:payload,...(resetPaths.size ? {agent_settings_reset:[...resetPaths]} : {})};
}

export async function mountNativeAgentSettings({el, checkedFetch, onLoaded = () => {}}) {
  const card = el('agent-native-settings-card');
  if (!card) return {};
  const status = el('set-agentNativeStatus'), effective = el('set-agentNativeEffective');
  const save = el('set-agentNativeSave'), reset = el('set-agentNativeReset');
  const tokenRows=el('tokenomics-native-fields'), tokenReadback=el('tokenomics-readback'), tokenStatus=el('tokenomics-status');
  const tokenSave=el('tokenomics-save'), tokenReset=el('tokenomics-reset');
  let state = {}, fields = CORE, controls = new Map(), loaded = false, pending = false;
  let baseline = '', generation = 0, controller = null, sessionId = get(KEYS.CURRENT_SESSION, '');
  let dirty = false, accountEpoch = 0;
  let loadedValues = new Map();
  const resets = new Set();
  const extra = document.createElement('div'); extra.dataset.agentAdditionalFields = '';
  card.querySelector('.settings-col').insertBefore(extra, status);
  const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'btn-secondary';
  retry.textContent = 'Reload saved settings'; retry.id = 'set-agentNativeReload';
  reset.after(retry);
  const details = document.createElement('details');
  const summary = document.createElement('summary'); summary.textContent = 'Effective policy and target';
  const provenance = document.createElement('p'); provenance.className='admin-toggle-sub';
  provenance.textContent='Values come from merged native configuration. Per-field winning layers and lock states are not yet available.';
  const readback = document.createElement('pre'); readback.style.whiteSpace = 'pre-wrap';
  details.append(summary, provenance, readback); effective.after(details);

  function currentDraft() {
    return JSON.stringify([...controls].map(([path,input]) => [path,input.value]));
  }
  function isDirty() { return dirty || loaded && currentDraft() !== baseline; }
  function setPending(value) {
    pending = value;
    save.disabled = value || !loaded; reset.disabled = value || !loaded; retry.disabled = value;
    const tokenFields=fields.filter(field=>field.path==='tokenomics.browser_snapshot_repeat' || field.path==='tokenomics.untracked_status' || field.path==='tokenomics.cargo_artifacts' || field.path==='tokenomics.unittest_success' || field.path==='tokenomics.git_diff_no_color' || field.path==='tokenomics.gcf_session_view' || field.path==='tokenomics.gcf_same_request_rows' || field.path==='tokenomics.gcf_argument_decode' || field.path==='tokenomics.gcf_filtering' || field.path==='tokenomics.gcf_memoization' || field.path==='tokenomics.json_whitespace' || field.path==='tokenomics.terminal_ansi' || field.path==='tokenomics.html_markdown' || field.path==='tokenomics.structured_search' || field.path==='tokenomics.repeated_lines' || field.path==='tokenomics.unified_diff' || field.path==='tokenomics.schema_annotations' || field.path==='tokenomics.toon' || field.path==='tokenomics.source_outlines' || field.path==='tokenomics.tap13' || field.path==='tokenomics.base64_lines' || field.path==='tokenomics.ndjson_logs' || field.path==='tokenomics.line_cap' || field.path==='tokenomics.line_cap_rows' || field.path==='tokenomics.json_shape' || field.path==='tokenomics.read_unnumbered' || field.path==='tokenomics.progress_redraw' || field.path==='tokenomics.lockfile_versions' || field.path==='tokenomics.markdown_slim' || field.path==='tokenomics.adaptive_verbosity');
    if(tokenSave) tokenSave.disabled=value || !loaded || !tokenFields.some(field=>field.available!==false);
    if(tokenReset) tokenReset.disabled=value || !loaded || !tokenFields.some(field=>field.available!==false || field.resettable===true);
    card.setAttribute('aria-busy', String(value));
    if (!loaded) card.querySelectorAll('input,select,textarea').forEach(input=>{input.disabled=true;});
    for (const [path,input] of controls) input.disabled = value || !loaded || fields.find(field=>field.path===path)?.available === false;
    card.querySelectorAll('[data-native-inherit]').forEach(button=>{
      const path=button.closest('[data-native-setting]')?.dataset.nativeSetting;
      const field=fields.find(item=>item.path===path);
      button.disabled=value || !loaded || (controls.get(path)?.disabled && field?.resettable !== true);
    });
  }
  function announce(message) { if (status) status.textContent = message; if (tokenStatus) tokenStatus.textContent = message; }
  function renderReadback(next) {
    const snapshot = next.agent_settings_effective;
    if (snapshot?.status === 'available' && snapshot.value) {
      const value = snapshot.value, c = value.compaction || {}, context = value.context || {};
      effective.textContent = 'Effective native values: ' + (context.hard ?? 'unknown') + ' hard / ' + (context.usable ?? 'unknown') +
        ' usable tokens · ' + (c.tailTurns ?? c.tail_turns ?? 'unknown') + ' tail turns · reserve ' + (c.reserved ?? 'inherited') +
        ' · ' + (sessionId ? 'selected chat' : 'default chat route') +
        (snapshot.projection_pending || value.projection_pending ? ' · saved policy pending a new worker generation' : '');
      readback.textContent = JSON.stringify({target: snapshot.target || value.target || {session_id:sessionId || null,scope:sessionId ? 'selected chat' : 'default route'}, ...value}, null, 2);
    } else {
      effective.textContent = 'Effective native values: ' + (snapshot?.status || 'pending') + (snapshot?.reason ? ' (' + snapshot.reason + ')' : '');
      readback.textContent = 'A ready native target is required. Reading settings does not start an engine.';
    }
    const saved = next.agent_settings_saved || next.agent_settings || {};
    const options = (Array.isArray(next.agent_settings_schema?.fields) ? next.agent_settings_schema.fields : fields)
      .filter(field => typeof field.path === 'string' && field.path.startsWith('tokenomics.'));
    const enabled = options.filter(field => field.type === 'boolean' && at(saved, field.path) === true).length;
    const unsupported = options.filter(field => field.available === false).length;
    const gcfRequested = at(saved, 'tokenomics.gcf_filtering') === true;
    const gcfField = options.find(field => field.path === 'tokenomics.gcf_filtering');
    const gcfReason = gcfField?.available === false ? (gcfField.reason || 'Unavailable for this account') : '';
    // The existing native readback does not report per-option eligibility.
    if (tokenReadback) tokenReadback.textContent = `Saved enabled: ${enabled} · Per-turn status: not reported · Unsupported controls: ${unsupported}` +
      (gcfRequested ? ` · GCF requested; ${gcfReason || 'eligibility is checked per admitted turn'}` : ' · GCF Off') +
      (enabled > 0 ? ' · Model fidelity and savings are unqualified' : '');
    effective.textContent += gcfRequested
      ? ` · GCF requested; ${gcfReason || 'codec, source and route eligibility is checked per admitted turn'}`
      : ' · GCF filtering off';
  }
  function buildControls() {
    extra.replaceChildren(); tokenRows?.replaceChildren(); controls = new Map();
    const groups = new Map();
    function groupFor(path) {
      const name = path.split('.')[0];
      if (!groups.has(name)) {
        const group = document.createElement('details');
        const heading = document.createElement('summary');
        heading.textContent = ({checkpoint:'Checkpoint section budgets',workflow:'Workflow limits',experimental:'Agent safeguards',dream:'Memory consolidation',distill:'Workflow packaging',watcher:'Workspace file watching',runtime_flags:'Runtime feature flags',never_ask:'Question behavior',command:'Custom commands',formatter:'Code formatters',lsp:'Language servers',instructions:'Instruction sources',agent:'Named agent profiles',model_groups:'Model groups'})[name] || name;
        group.append(heading); extra.append(group); groups.set(name,group);
      }
      return groups.get(name);
    }
    CORE.forEach(field => { const row = el(field.id)?.closest('.settings-row'); if (row) row.hidden = !fields.some(item => item.path === field.path); });
    fields.forEach(field => {
      // The validated object editor owns its child keys; avoid two writers.
      if (fields.some(parent => parent.type === 'object' && field.path.startsWith(parent.path + '.'))) return;
      const core = CORE.find(item=>item.path === field.path);
      let input = core && el(core.id), row = input?.closest('.settings-row');
      if (!row) {
        row = document.createElement('div'); row.className = 'settings-row';
        const label = document.createElement('label'); label.className = 'settings-label';
        label.textContent = field.path === 'tokenomics.untracked_status' ? 'Compact untracked-status-shaped output' : field.path === 'tokenomics.browser_snapshot_repeat' ? 'Repeated Browser snapshot text' : field.path === 'tokenomics.cargo_artifacts' ? 'Compact Cargo artifact summary' : field.path === 'tokenomics.unittest_success' ? 'Compact unittest success output' : field.path === 'tokenomics.git_diff_no_color' ? 'Git diff no-color substitution' : field.path === 'tokenomics.adaptive_verbosity' ? 'Adaptive concision' : field.path === 'tokenomics.markdown_slim' ? 'Compact WebFetch Markdown' : field.path === 'tokenomics.lockfile_versions' ? 'Compact npm version lookup' : field.path === 'tokenomics.progress_redraw' ? 'Compact progress redraws' : field.path === 'tokenomics.read_unnumbered' ? 'Compact file-read lines' : field.path === 'tokenomics.json_shape' ? 'JSON structure view' : field.path === 'tokenomics.line_cap' ? 'Lossy search view' : field.path === 'tokenomics.line_cap_rows' ? 'Search result lines to retain' : field.path === 'tokenomics.ndjson_logs' ? 'Compact structured logs' : field.label || field.path; input = document.createElement('input');
        input.id = 'set-native-' + field.path.replace(/[^a-z0-9]/gi,'-'); label.htmlFor = input.id;
        row.append(label,input);
        if(field.path.startsWith('tokenomics.') && tokenRows) tokenRows.append(row); else groupFor(field.path).append(row);
      }
      row.dataset.nativeSetting = field.path;
      if (field.type === 'boolean' || field.enum) {
        const select = document.createElement('select'); select.id = input.id; select.className = 'settings-select';
        const choices = field.enum ? [['','Inherit'],...field.enum.map(value=>[String(value),String(value)])] : field.path.startsWith('tokenomics.') ? [['','Default Off'],['true','On'],['false','Off']] : [['','Inherit'],['true','Enabled'],['false','Disabled']];
        choices.forEach(([value,label])=>{const option = document.createElement('option'); option.value=value; option.textContent=label; select.append(option);});
        input.replaceWith(select); input = select;
      } else {
        if (field.type === 'object' || field.type === 'json') {
          const area=document.createElement('textarea'); area.id=input.id; area.rows=4;
          input.replaceWith(area); input=area;
        }
        if (input.tagName === 'INPUT') input.type = field.type === 'integer' || field.type === 'number' ? 'number' : 'text';
        input.classList.add('settings-select'); input.placeholder = field.path === 'tokenomics.line_cap_rows' ? 'Default 40' : 'Inherit';
        if (field.min != null) input.min=field.min; else input.removeAttribute('min');
        if (field.max != null) input.max=field.max; else input.removeAttribute('max');
        input.step = field.type === 'integer' ? '1' : 'any';
      }
      input.dataset.nativeSetting = field.path;
      input.disabled = field.available === false;
      const info = document.createElement('span'); info.className='admin-toggle-sub';
      const timing = field.apply === 'next_generation' ? 'next worker generation' : field.apply === 'next_scheduler_wake' ? 'next scheduler wake' : 'next admitted turn';
      info.dataset.nativeFieldInfo=''; info.textContent = field.available === false ? (field.reason || 'Unavailable') : (field.path === 'tokenomics.untracked_status' ? 'For supported complete simple ?? lines, preserve directory slashes; unsupported output stays raw and the exact original remains in History. This does not establish repository state' : field.path === 'tokenomics.cargo_artifacts' ? 'Summarize supported complete Cargo artifact records, omitting known metadata, profile and features; diagnostics and unsupported output stay raw, with the exact original in History' : field.path === 'tokenomics.unittest_success' ? 'For supported complete unittest success output, shorten progress dots while preserving the summary; unsupported output stays raw and the exact original remains in History' : field.path === 'tokenomics.git_diff_no_color' ? 'For the exact eligible command git diff, substitute git diff --no-color; this can override color configuration and may save no output' : field.path === 'tokenomics.adaptive_verbosity' ? 'For Normal style only, advise concision at high estimated text-context pressure on qualified targets; otherwise the current style is unchanged' : field.path === 'tokenomics.markdown_slim' ? 'Omit standalone comments and excess blanks from eligible WebFetch Markdown while preserving supported code and links; unsupported output stays raw and the original remains in History' : field.path === 'tokenomics.lockfile_versions' ? 'For eligible complete npm v2/v3 lockfiles, show versions without integrity, resolution or dependency-graph data; the exact original remains in History' : field.path === 'tokenomics.progress_redraw' ? 'Omit intermediate progress redraws from eligible model views; the exact original remains in History' : field.path === 'tokenomics.read_unnumbered' ? 'For qualified complete file reads, remove generated line gutters from the model view; range and text remain, with the original in History' : field.path === 'tokenomics.json_shape' ? 'Show JSON structure with keys retained and scalar values omitted; the full original remains in History' : field.path === 'tokenomics.line_cap' ? 'Show a shorter search result to the model; the full original remains in History' : field.path === 'tokenomics.line_cap_rows' ? 'Retained result lines, from 1 to 100; default 40' : field.path === 'tokenomics.ndjson_logs' ? 'LF-separated records with the same fields and string log values' : field.help || field.path) + ' · ' + (field.scope || 'account') + ' · ' + timing + (field.gate ? ' · enabled by ' + field.gate : '') + (field.source ? ' · source: ' + field.source : '');
      row.querySelectorAll('[data-native-field-info], [data-native-inherit], [data-memory-owner-link]').forEach(node=>node.remove());
      if (field.path.startsWith('distill.')) {
        const intent = state.agent_settings_distill_intent;
        const legacy = at(state.agent_settings_saved || {},field.path);
        const label = intent?.state === 'legacy_unactivated' ? `Legacy saved value ${legacy === undefined ? '(none)' : JSON.stringify(legacy)} is not active; choose a value or reset explicitly` : intent?.state === 'active' ? 'Saved automatic request is On' : 'Saved automatic request is Off';
        info.textContent = `${label}. Eligibility is not evaluated here; the scheduler checks the profile, configured teacher and current repeated sources on a later wake.`;
      }
      input.setAttribute('aria-describedby', input.id + '-help'); info.id=input.id+'-help'; row.append(info);
      if (field.path === 'dream.auto') {
        const open=document.createElement('button');open.type='button';open.className='btn-secondary';open.dataset.memoryOwnerLink='';open.textContent='Open Memory consolidation';
        open.onclick=()=>{
          const modal=document.getElementById('memory-modal');
          if (!modal || modal.classList.contains('hidden')) (document.getElementById('tool-memory-btn') || document.getElementById('rail-memory'))?.click();
          document.querySelector('.memory-tab[data-memory-tab="settings"]')?.click();
          document.getElementById('memory-policy-consolidation-toggle')?.focus();
        };row.append(open);
      }
      if(field.path !== 'tokenomics.gcf_filtering') {
        const inherit = document.createElement('button'); inherit.type='button'; inherit.textContent='Inherit'; inherit.className='btn-secondary'; inherit.dataset.nativeInherit=''; inherit.disabled=input.disabled && field.resettable !== true;
        inherit.onclick=()=>{input.value='';resets.add(field.path);dirty=true;announce('Unsaved changes · ' + field.path + ' will inherit after Save');};row.append(inherit);
      }
      input.addEventListener('input',()=>{dirty=true;announce('Unsaved settings · next admitted turn after Save');});
      input.addEventListener('change',()=>{dirty=true;}); controls.set(field.path,input);
    });
  }
  function showSaved(next) {
    const saved = next.agent_settings_saved || (next.agent_settings_override === false ? {} : next.agent_settings || {});
    for (const [path,input] of controls) {
      const value=at(saved,path);
      input.value = path.startsWith('distill.') && next.agent_settings_distill_intent?.state === 'legacy_unactivated' ? '' : value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    }
    loadedValues = new Map([...controls].map(([path,input]) => [path,input.value]));
    dirty=false;resets.clear();loaded=true;baseline=currentDraft();
    if(tokenReadback && !fields.some(field=>field.path==='tokenomics.gcf_filtering')) tokenReadback.textContent='This preference is unavailable for this account.';
    announce(next.agent_settings_override ? 'Saved account overrides · each field shows when it applies' : 'Inherited native defaults · each field shows when it applies');
  }
  async function reload(force = false) {
    const id=++generation;
    if(controller)controller.abort();controller=new AbortController();
    try {
      const suffix=sessionId ? '?session_id='+encodeURIComponent(sessionId) : '';
      const response=await checkedFetch('/api/auth/settings'+suffix,{credentials:'same-origin',signal:controller.signal});
      const next=await response.json();if(id!==generation)return state;
      renderReadback(next);onLoaded(next);
      // A target change may refresh readback while the mutation is in flight.
      // Only that mutation's forced reload may replace or unlock its editors.
      if(pending && !force)return state;
      if(!force && isDirty()) {announce('Unsaved changes preserved · effective target refreshed. Save may require reloading after another edit.');return state;}
      state=next;
      const schema=next.agent_settings_schema;
      if(schema?.fields)fields=schema.fields.map(field=>({...CORE.find(core=>core.path===field.path),...field}));
      catalogFields=fields;buildControls();showSaved(next);setPending(false);
      document.dispatchEvent(new CustomEvent('openclank:settings-catalog-changed'));
      return next;
    } catch(error) {
      if(error.name==='AbortError')return state;
      announce('Native settings read failed: '+error.message+' · Reload to retry.');setPending(false);return state;
    }
  }
  async function mutate(payload) {
    if(!loaded||pending)return;
    const mutationAccountEpoch=accountEpoch;
    setPending(true);announce('Saving settings…');
    if(state.agent_settings_revision != null)payload.agent_settings_expected_revision=state.agent_settings_revision;
    try {
      await checkedFetch('/api/auth/settings',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(mutationAccountEpoch!==accountEpoch)return;
      dirty=false;loaded=false;baseline='';await reload(true);
    } catch(error) {if(mutationAccountEpoch===accountEpoch)announce('Settings not saved: '+error.message+' · Your draft is preserved.');}
    finally {if(mutationAccountEpoch===accountEpoch)setPending(false);}
  }
  save.onclick=()=>{
    try {
      const saved=state.agent_settings_saved || state.agent_settings || {};
      void mutate(buildAgentSettingsMutation(fields,controls,saved,resets,loadedValues));
    }catch(error){announce('Invalid native settings: '+error.message);}
  };
  reset.onclick=()=>{void mutate({agent_settings:null});};
  if(tokenSave) tokenSave.onclick=()=>save.click();
  if(tokenReset) tokenReset.onclick=()=>{for(const [path,input] of controls)if(path.startsWith('tokenomics.')){input.value='';resets.add(path);}dirty=true;save.click();};
  retry.onclick=()=>{
    if(isDirty()&&!window.confirm('Discard the unsaved native settings draft and reload saved values?'))return;
    void reload(true);
  };
  document.addEventListener('openclank:auth-context-changed',()=>{
    accountEpoch++;generation++;controller?.abort();state={};loaded=false;dirty=false;baseline='';resets.clear();
    controls.forEach(input=>{input.value='';});setPending(false);
    if(tokenReadback)tokenReadback.textContent='Loading account preference…';
    void reload(true);
  });
  window.addEventListener('openclank:default-chat-changed',()=>{void reload();});
  document.addEventListener('odysseus:session-selected',event=>{sessionId=event.detail?.sessionId || '';void reload();});
  document.addEventListener('odysseus:model-picked',()=>{void reload();});
  setPending(false);
  return reload(true);
}
