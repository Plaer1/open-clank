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
].map(([path, type, id, label, min, max]) => ({path, type, id, label, min, max, available:true, scope:'account', apply:'next_admission'}));
let catalogFields = CORE;

export function nativeSettingControls() {
  return catalogFields.map(field => ({path:field.path, supported:field.available !== false,
    availability:field.available === false ? 'unavailable' : 'available', writable:true,
    referencePaths:Array.isArray(field.reference_paths) ? field.reference_paths : [], scope:field.scope || 'account',
    destination:{panel:'ai',field:field.path}, reason:field.reason || ''}));
}

export function agentSettingsCatalogEntries() {
  return catalogFields.map(field => ({
    identity: 'setting:' + field.path,
    token: 'settings ai ' + field.path,
    aliases: [], help: field.available === false && field.resettable === true
      ? 'Reset only: ' + (field.reason || field.help || field.label || field.path)
      : field.help || field.label || field.path,
    category: 'Settings', origin: 'host', executor: 'settings',
    scope: field.scope || 'account', destination: {panel:'ai', field:field.path},
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

export async function mountNativeAgentSettings({el, checkedFetch, onLoaded = () => {}}) {
  const card = el('agent-native-settings-card');
  if (!card) return {};
  const status = el('set-agentNativeStatus'), effective = el('set-agentNativeEffective');
  const save = el('set-agentNativeSave'), reset = el('set-agentNativeReset');
  let state = {}, fields = CORE, controls = new Map(), loaded = false, pending = false;
  let baseline = '', generation = 0, controller = null, sessionId = get(KEYS.CURRENT_SESSION, '');
  let dirty = false;
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
    card.setAttribute('aria-busy', String(value));
    if (!loaded) card.querySelectorAll('input,select,textarea').forEach(input=>{input.disabled=true;});
    for (const [path,input] of controls) input.disabled = value || !loaded || fields.find(field=>field.path===path)?.available === false;
    card.querySelectorAll('[data-native-inherit]').forEach(button=>{
      const path=button.closest('[data-native-setting]')?.dataset.nativeSetting;
      const field=fields.find(item=>item.path===path);
      button.disabled=value || !loaded || (controls.get(path)?.disabled && field?.resettable !== true);
    });
  }
  function announce(message) { if (status) status.textContent = message; }
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
  }
  function buildControls() {
    extra.replaceChildren(); controls = new Map();
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
        label.textContent = field.label || field.path; input = document.createElement('input');
        input.id = 'set-native-' + field.path.replace(/[^a-z0-9]/gi,'-'); label.htmlFor = input.id;
        row.append(label,input); groupFor(field.path).append(row);
      }
      row.dataset.nativeSetting = field.path;
      if (field.type === 'boolean' || field.enum) {
        const select = document.createElement('select'); select.id = input.id; select.className = 'settings-select';
        const choices = field.enum ? [['','Inherit'],...field.enum.map(value=>[String(value),String(value)])] : [['','Inherit'],['true','Enabled'],['false','Disabled']];
        choices.forEach(([value,label])=>{const option = document.createElement('option'); option.value=value; option.textContent=label; select.append(option);});
        input.replaceWith(select); input = select;
      } else {
        if (field.type === 'object' || field.type === 'json') {
          const area=document.createElement('textarea'); area.id=input.id; area.rows=4;
          input.replaceWith(area); input=area;
        }
        if (input.tagName === 'INPUT') input.type = field.type === 'integer' || field.type === 'number' ? 'number' : 'text';
        input.classList.add('settings-select'); input.placeholder = 'Inherit';
        if (field.min != null) input.min=field.min; else input.removeAttribute('min');
        if (field.max != null) input.max=field.max; else input.removeAttribute('max');
        input.step = field.type === 'integer' ? '1' : 'any';
      }
      input.dataset.nativeSetting = field.path;
      input.disabled = field.available === false;
      const info = document.createElement('span'); info.className='admin-toggle-sub';
      const timing = field.apply === 'next_generation' ? 'next worker generation' : 'next admitted turn';
      info.dataset.nativeFieldInfo=''; info.textContent = field.available === false ? (field.reason || 'Unavailable') : (field.help || field.path) + ' · ' + (field.scope || 'account') + ' · ' + timing + (field.gate ? ' · enabled by ' + field.gate : '') + (field.source ? ' · source: ' + field.source : '');
      row.querySelectorAll('[data-native-field-info], [data-native-inherit]').forEach(node=>node.remove());
      input.setAttribute('aria-describedby', input.id + '-help'); info.id=input.id+'-help'; row.append(info);
      const inherit = document.createElement('button'); inherit.type='button'; inherit.textContent='Inherit'; inherit.className='btn-secondary'; inherit.dataset.nativeInherit=''; inherit.disabled=input.disabled && field.resettable !== true;
      inherit.onclick=()=>{input.value='';resets.add(field.path);dirty=true;announce('Unsaved changes · ' + field.path + ' will inherit after Save');};row.append(inherit);
      input.addEventListener('input',()=>{dirty=true;announce('Unsaved settings · next admitted turn after Save');});
      input.addEventListener('change',()=>{dirty=true;}); controls.set(field.path,input);
    });
  }
  function showSaved(next) {
    const saved = next.agent_settings_saved || (next.agent_settings_override === false ? {} : next.agent_settings || {});
    for (const [path,input] of controls) {
      const value=at(saved,path);
      input.value = value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    }
    dirty=false;resets.clear();loaded=true;baseline=currentDraft();
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
    setPending(true);announce('Saving settings…');
    if(state.agent_settings_revision != null)payload.agent_settings_expected_revision=state.agent_settings_revision;
    try {
      await checkedFetch('/api/auth/settings',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      dirty=false;loaded=false;baseline='';await reload(true);
    } catch(error) {announce('Settings not saved: '+error.message+' · Your draft is preserved.');}
    finally {setPending(false);}
  }
  save.onclick=()=>{
    try {
      const payload={compaction:{},checkpoint:{}}, resetPaths=new Set(resets);
      const saved=state.agent_settings_saved || state.agent_settings || {};
      for(const field of fields) {
        const input=controls.get(field.path);if(!input||field.available===false)continue;
        const value=parseAgentField(field,field.type === 'string' ? input.value : input.value.trim());
        if(value===undefined) {if(at(saved,field.path)!==undefined)resetPaths.add(field.path);}
        else {put(payload,field.path,value);resetPaths.delete(field.path);}
      }
      void mutate({agent_settings:payload,...(resetPaths.size ? {agent_settings_reset:[...resetPaths]} : {})});
    }catch(error){announce('Invalid native settings: '+error.message);}
  };
  reset.onclick=()=>{void mutate({agent_settings:null});};
  retry.onclick=()=>{
    if(isDirty()&&!window.confirm('Discard the unsaved native settings draft and reload saved values?'))return;
    void reload(true);
  };
  window.addEventListener('openclank:default-chat-changed',()=>{void reload();});
  document.addEventListener('odysseus:session-selected',event=>{sessionId=event.detail?.sessionId || '';void reload();});
  document.addEventListener('odysseus:model-picked',()=>{void reload();});
  setPending(false);
  return reload(true);
}
