export function indexUpdateLabel(value){
  if(typeof value!=='string'||!value)return '';
  const date=new Date(value);return Number.isFinite(date.getTime())?'Last index update '+date.toLocaleTimeString():'';
}
// Phase-local work counters, never an overall indexing percentage.
export function indexPhaseLabel(progress={}){
  const labels={inventory:'Discovering files',verification:'Verifying unchanged files',extraction:'Extracting source',dependency_resolution:'Resolving dependencies',visual_summary:'Preparing graph summary',sealing:'Sealing index',publication:'Publishing index',ready:'Index run complete'};
  const phase=labels[progress.phase];
  if(!phase){const done=progress.completed_files,total=progress.total_files;return Number.isSafeInteger(done)&&Number.isSafeInteger(total)&&total>0?`${done}/${total} files processed · phase unavailable`:'';}
  const completed=progress.phase_completed,total=progress.phase_total,unit=['files','entries','references','links'].includes(progress.phase_unit)?progress.phase_unit:'items';
  const counts=Number.isSafeInteger(completed)&&completed>=0?(Number.isSafeInteger(total)&&total>=completed?`${completed}/${total}`:String(completed))+' '+unit+' processed':'';
  return [phase,counts].filter(Boolean).join(' · ');
}
// One disposable subscriber to the existing index owner; never query-side admission.
export function indexPresentation(index = {}) {
  const lifecycle = String(index.lifecycle || 'unavailable');
  const pending = ['starting', 'admitted', 'queued', 'building', 'running'].includes(lifecycle);
  const usable = Boolean(index.head_generation) && ['ready', 'failed'].includes(lifecycle);
  const partial = index.freshness === 'partial';
  const recovering = lifecycle === 'unavailable' && index.recovering === true;
  const progress = index.progress || {};
  const detail = index.error?.message ? ` ${index.error.message}` : '';
  const code = index.error?.code ? ` (${index.error.code})` : '';
  const messages = {
    starting:'Starting code index', admitted:'Starting code index', queued:'Code index queued',
    building:'Indexing code', running:'Indexing code',
    ready:partial ? 'Ready with partial coverage; indexed evidence is not verified-current source' : 'Code index ready; source actions revalidate current bytes',
    failed:usable ? 'Rebuild failed; previous indexed head remains available and is not current' : 'Code indexing failed; no usable head',
    missing:'Code index is unindexed; retry indexing or choose another folder',
    erased:'Code index was erased; only explicit readmission can index this folder again',
    denied:'Workspace access denied or revoked; choose an authorized folder',
    cancelled:'Code indexing was cancelled; retry explicitly or choose another folder',
    unavailable:recovering ? 'Code index status temporarily unavailable; rechecking observation' : 'Code index provider unavailable; recheck or choose another folder',
  };
  const progressText = pending || ['failed','cancelled'].includes(lifecycle) ? indexPhaseLabel(progress) : '';
  return { lifecycle, pending, usable, partial, recovering,
    text:(messages[lifecycle] || `Code index ${lifecycle}`) + (progressText?' · '+progressText:'') + (progress.heartbeat_at&&indexUpdateLabel(progress.heartbeat_at)?' · '+indexUpdateLabel(progress.heartbeat_at):'') + code + detail };
}

export function observeCodeIndex({ request, isCurrent = () => true, onStatus = () => {},
  onDispose = () => {},
  observe = true, schedule = (fn, delay) => setTimeout(fn, delay), unschedule = clearTimeout,
  initialDelay = 1000, maxDelay = 10000, automatic = false, verifySeconds = 60, cooldownSeconds = 15, debounceMs = 1000 }) {
  let disposed = false, timer = null, inFlight = false, delay = initialDelay, latest = null;
  let pendingObserved = false, visible=true, dirty=false,lastVerify=0,verifiedAt=null,lastHead=null,verificationPending=false;
  // Only a validated event observed after this failure may grant one ensure.
  // The latch survives transport outages; ambient rechecks cannot restart work.
  let failureLatch=false,failureToken=null,sourceSerial=0,failureWatermark=0,sourceGrant=null;
  let tuning={automatic,verifySeconds,cooldownSeconds,debounceMs};
  const online=()=>typeof navigator==='undefined'||navigator.onLine!==false;
  const mayVerify=()=>!failureLatch&&tuning.automatic&&visible&&online()&&(latest?.head_generation||lastHead)&&!['erased','denied','missing','cancelled','failed'].includes(latest.lifecycle)&&!['unsupported','reindex_required','invalid_response'].includes(latest.error?.code);
  const mayUseSourceGrant=()=>Boolean(sourceGrant&&sourceGrant.failure===failureToken&&latest?.lifecycle==='failed'
    &&tuning.automatic&&visible&&online()&&!['unsupported','reindex_required','invalid_response'].includes(latest.error?.code));
  async function verifySourceChange(){
    if(!active()||inFlight||!mayUseSourceGrant())return;
    if(Date.now()-lastVerify<tuning.cooldownSeconds*1000){scheduleNext();return;}
    // Consume before the request. Neither its failed result nor a transport
    // error may turn this event into a second automatic initiation.
    sourceGrant=null;failureToken=null;dirty=false;lastVerify=Date.now();
    verificationPending=true;return perform('open');
  }
  function scheduleNext(){
    if(timer!==null)unschedule(timer);timer=null;if(!active()||!observe)return;
    if(indexPresentation(latest||{}).pending||latest?.recovering){timer=schedule(()=>{timer=null;void perform('status');},delay);delay=Math.min(maxDelay,delay*1.5);}
    else if(mayUseSourceGrant()){timer=schedule(()=>{timer=null;void verifySourceChange();},Math.max(tuning.debounceMs,tuning.cooldownSeconds*1000-(Date.now()-lastVerify)));}
    else if(mayVerify()){const wait=dirty?Math.max(tuning.debounceMs,tuning.cooldownSeconds*1000-(Date.now()-lastVerify)):Math.max(tuning.verifySeconds*1000,delay);
      timer=schedule(()=>{timer=null;void verify();},wait);}
  }
  async function verify(manual=false){
    if(!active()||!(latest?.head_generation||lastHead)||['erased','denied','missing'].includes(latest.lifecycle))return;
    if(inFlight||indexPresentation(latest).pending){dirty=true;return;}
    if(!manual&&(!mayVerify()||Date.now()-lastVerify<tuning.cooldownSeconds*1000)){scheduleNext();return;}
    sourceGrant=null;failureToken=null;dirty=false;lastVerify=Date.now();verificationPending=true;return perform('open');
  }
  const lifecycles = new Set(['starting','admitted','queued','building','running','ready','failed','missing','erased','denied','cancelled','unavailable']);
  const validStatus = index => index && typeof index === 'object' && !Array.isArray(index) && lifecycles.has(index.lifecycle);
  const transientStatusError = error => !['denied','revoked','access_revoked','permission_denied','erased','cancelled','failed','invalid_response'].includes(error?.code)
    && ([408,429,502,503,504].includes(error?.status) || (error instanceof TypeError && error.status == null));
  const releaseRequest = () => { void Promise.resolve().then(() => request('release')).catch(() => {}); };
  const release = () => {
    if (disposed) return;
    disposed = true;sourceGrant=null;failureToken=null;
    if (timer !== null) unschedule(timer);
    timer = null;
    releaseRequest();
    onDispose();
  };
  const active = () => {
    if (!disposed && !isCurrent()) release();
    return !disposed;
  };
  const errorStatus = error => ({ lifecycle:[401,403,404].includes(error.status) ? 'denied' : 'unavailable',
    error:{code:error.code || 'index_unavailable',message:error.message || 'Code index unavailable'} });
  async function perform(event) {
    if (!active() || inFlight) return;
    inFlight = true;
    let index, failed = false;
    try {
      try { if(event==='open')verificationPending=true;index = await request(event); }
      catch (error) {
        // An erased ensure is rejected. Status distinguishes its tombstone
        // without re-admitting or issuing a second ensure.
        if (event === 'open' && error.status === 409) index = await request('status');
        else throw error;
      }
    } catch (error) {
      failed = true;
      index = errorStatus(error);
      // Only observation of a previously witnessed pending build may recover.
      // Keep the same subscription; never repeat open/admit or infer a head.
      if (event === 'status' && pendingObserved && transientStatusError(error)) {
        // A transport outage is not a native job deadline. Continue at capped
        // backoff until a real status, revocation, scope loss or disposal.
        index.recovering = true;
      } else pendingObserved = false;
    }
    finally { inFlight = false; }
    if (!active()) {
      // Disposal can reach the owner before the in-flight open/admit does.
      if (event === 'open' || event === 'admit') releaseRequest();
      return;
    }
    const wasPending=pendingObserved,previous=latest;
    if (!validStatus(index)) {
      index = {lifecycle:'unavailable',error:{code:'invalid_response',message:'Invalid index status'}};
      pendingObserved = false;
    } else if (!failed) {
      pendingObserved = indexPresentation(index).pending;
      // A server response cannot grant the local transport-recovery marker.
      index = {...index};delete index.recovering;
    }
    if(failed){dirty=false;delay=Math.min(300000,Math.max(initialDelay,delay*2));}
    else {if(!indexPresentation(index).pending)delay=initialDelay;
      if(index.head_generation)lastHead=index.head_generation;
      if(['denied','erased','missing'].includes(index.lifecycle))lastHead=null;
      if(index.lifecycle==='failed'){
        failureLatch=true;
        if(!failureToken||previous?.lifecycle!=='failed'||previous?.run_id!==index.run_id){
          failureToken=Object.freeze({});failureWatermark=sourceSerial;sourceGrant=null;
        }
      }else if(index.lifecycle!=='unavailable'){
        failureToken=null;sourceGrant=null;
        if(index.lifecycle==='ready')failureLatch=false;
      }
      if(['failed','cancelled'].includes(index.lifecycle)){dirty=false;verificationPending=false;}
      if(index.lifecycle==='ready'&&(event==='open'||verificationPending||wasPending)){verifiedAt=new Date().toISOString();verificationPending=false;}
    }
    latest = {...index,verified_at:verifiedAt,previous_head_generation:!index.head_generation?lastHead:null};
    onStatus(latest);
    if (!active()) return;
    scheduleNext();
  }
  release.verify=()=>verify(true);
  release.captureSourceChange=()=>{
    const serial=++sourceSerial;
    return active()&&latest?.lifecycle==='failed'&&failureToken
      ? Object.freeze({failure:failureToken,serial}):null;
  };
  release.sourceChanged=ticket=>{
    if(!active()||latest?.lifecycle!=='failed'||!ticket||ticket.failure!==failureToken
      ||ticket.serial<=failureWatermark||ticket.serial>sourceSerial)return;
    failureWatermark=ticket.serial;
    sourceGrant=ticket;scheduleNext();
  };
  release.dirty=()=>{dirty=true;scheduleNext();};
  release.setVisible=value=>{const next=value===true;if(visible===next)return;visible=next;if(visible)dirty=true;scheduleNext();};
  release.configure=patch=>{tuning={...tuning,...patch};scheduleNext();};

  release.refresh = () => { if (timer !== null) unschedule(timer); timer = null; return perform('status'); };
  // This is intentionally a separate user action, never part of polling.
  release.readmit = () => latest?.lifecycle === 'erased' ? perform('admit') : Promise.resolve();
  void perform('open');
  return release;
}
