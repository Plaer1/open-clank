// Explicit Settings read/save only; never starts or polls an indexing job.
export function parseMaxThreads(value) {
  if (typeof value !== 'string' || !/^(?:[1-9]|[12][0-9]|3[0-2])$/.test(value)) return null;
  return Number(value);
}

const RESOURCE_FIELDS = [
  {name:'max_files', key:'code_index_max_files', label:'Max selected files per code folder', max:10000},
  {name:'max_source_bytes', key:'code_index_max_source_bytes', label:'Total acquired source bytes per code folder', max:67108864},
  {name:'max_file_bytes', key:'code_index_max_file_bytes', label:'Bytes per source file', max:524288},
  {name:'max_generation_bytes', key:'code_index_max_generation_bytes', label:'Derived generation disk limit (bytes; default 2 GiB, maximum 8 GiB)', min:4096, step:4096, max:8589934592, default:2147483648},
];
const positive = (value,max) => typeof value==='string' && /^[1-9][0-9]*$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value)<=max ? Number(value) : null;
const limitsSummary = limits => limits ? `${integer(limits.max_files)} files, ${integer(limits.max_source_bytes)} total bytes, ${integer(limits.max_file_bytes)} bytes/file, ${integer(limits.max_generation_bytes)} bytes/generation` : 'unknown';
const integer = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unknown';
export function schedulerSummary(data) {
  const scheduler = data?.code_index_scheduler;
  if (!scheduler || !['available', 'unavailable'].includes(scheduler.native_status)
      || !['pending', 'draining', 'applied'].includes(scheduler.status)) return 'Indexing status unavailable. Refresh to try again.';
  const desired = Number.isInteger(data.code_index_max_threads) && data.code_index_max_threads >= 1 && data.code_index_max_threads <= 32
    ? data.code_index_max_threads : 'unknown';
  return `Saved/requested threads: ${desired}. ${scheduler.status === 'applied' ? 'Applied' : scheduler.status === 'draining' ? 'Draining active work before applying' : 'Pending application'}. Native: ${scheduler.native_status}. Effective: ${integer(scheduler.effective)}; active: ${integer(scheduler.active)}; queued: ${integer(scheduler.queued)}. Revision: ${integer(data.code_index_settings_revision)}; native revision: ${integer(scheduler.native_revision)}; effective revision: ${integer(scheduler.effective_revision)}. Drain pending: ${scheduler.drain_pending === true ? 'yes' : scheduler.drain_pending === false ? 'no' : 'unknown'}. Resource limits requested: ${limitsSummary(scheduler.desired_limits)}. Effective: ${limitsSummary(scheduler.effective_limits)}.`;
}

export function mountCodeIndexSettings(root, {fetcher = globalThis.fetch} = {}) {
  if (!root || root.dataset.codeIndexMounted) return;
  root.dataset.codeIndexMounted = '1';
  const input = root.querySelector('[data-code-index-threads]');
  const save = root.querySelector('[data-code-index-save]');
  const refresh = root.querySelector('[data-code-index-refresh]');
  const status = root.querySelector('[data-code-index-settings-status]');
  const resourceInputs = RESOURCE_FIELDS.map(field => {
    const label=document.createElement('label');label.className='settings-label';
    const control=document.createElement('input');control.id=`code-index-${field.name}`;control.type='number';
    control.min=String(field.min??1);control.max=String(field.max);control.step=String(field.step??1);control.inputMode='numeric';control.value=String(field.default??field.max);
    control.setAttribute('aria-describedby','code-index-settings-status');
    label.htmlFor=control.id;label.textContent=field.label;
    save.before(label,control);return {...field,control};
  });
  const explanation=document.createElement('p');explanation.className='admin-toggle-sub';
  explanation.textContent='Resource limits apply after active indexing drains. File-count and source-byte limits change the producer identity; the storage-only generation disk limit does not. Existing source and published generations are preserved. Per-file bytes cannot exceed total source bytes. The generation disk limit bounds derived index storage; it does not reserve free disk space.';
  save.before(explanation);
  const hardStatus=document.createElement('p');hardStatus.className='admin-toggle-sub';hardStatus.textContent='Native hard quotas: readback pending.';status.after(hardStatus);
  let busy = false;
  let forbidden = false;
  const controls = () => { for(const field of resourceInputs)field.control.disabled=busy||forbidden; input.disabled = busy || forbidden; save.disabled = busy || forbidden; refresh.disabled = busy; };
  const request = async (method) => {
    if (busy) return;
    const value = method === 'POST' ? parseMaxThreads(input.value) : null;
    if (method === 'POST' && value === null) {
      status.textContent = 'Enter a whole number from 1 to 32.';
      input.setAttribute('aria-invalid', 'true'); input.focus(); return;
    }
    const values={code_index_max_threads:value};
    if(method==='POST'){
      for(const field of resourceInputs){
        const parsed=positive(field.control.value,field.max);
        field.control.removeAttribute('aria-invalid');
        if(parsed===null||parsed<(field.min??1)||parsed%(field.step??1)!==0){status.textContent=`${field.label}: enter a whole number from ${field.min??1} to ${field.max}${field.step?' in increments of '+field.step:''}.`;field.control.setAttribute('aria-invalid','true');field.control.focus();return;}
        values[field.key]=parsed;
      }
      if(values.code_index_max_file_bytes>values.code_index_max_source_bytes){status.textContent='Per-file bytes must not exceed total source bytes.';resourceInputs[2].control.focus();return;}
    }
    input.removeAttribute('aria-invalid');
    busy = true; controls();
    status.textContent = method === 'POST' ? 'Saving indexing preference…' : 'Reading indexing status…';
    try {
      const response = await fetcher('/api/auth/settings', {
        method, credentials: 'same-origin',
        ...(method === 'POST' ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)} : {}),
      });
      if (!response.ok) {
        if (response.status === 403) forbidden = true;
        status.textContent = response.status === 403 ? 'Only an administrator can change this app-wide preference.'
          : response.status === 401 ? 'Sign in to read indexing settings.'
            : response.status === 400 ? 'The indexing preference was rejected. Refresh and check the whole-number limits and per-file/total relationship.'
              : 'Indexing settings unavailable. Refresh to try again.';
        return;
      }
      const data = await response.json();
      if (Number.isInteger(data.code_index_max_threads) && data.code_index_max_threads >= 1 && data.code_index_max_threads <= 32) input.value = String(data.code_index_max_threads);
      for(const field of resourceInputs)if(Number.isSafeInteger(data[field.key])&&data[field.key]>=(field.min??1)&&data[field.key]<=field.max&&data[field.key]%(field.step??1)===0)field.control.value=String(data[field.key]);
      const hard=data.code_index_scheduler?.hard_limits;
      hardStatus.textContent=hard ? `Native hard resource ceilings: ${limitsSummary(hard)}. Read-only quotas: ${integer(hard.max_queued_jobs)} queued jobs; ${integer(hard.max_descriptor_bytes)} descriptor bytes; ${integer(hard.max_retained_data_bytes)} retained data bytes (not process RSS); ${integer(hard.max_source_records)} evidence records; ${integer(hard.max_resolution_records)} resolution records.` : 'Native hard quotas unavailable; saved limits remain pending.';
      status.textContent = schedulerSummary(data);
    } catch (_) { status.textContent = 'Indexing settings unavailable. Refresh to try again.'; }
    finally { busy = false; controls(); }
  };
  save.addEventListener('click', () => void request('POST'));
  refresh.addEventListener('click', () => void request('GET'));
  for(const field of resourceInputs)field.control.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();void request('POST');}});
  input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); void request('POST'); } });
  void request('GET');
}
