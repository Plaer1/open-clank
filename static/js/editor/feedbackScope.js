import { wireDialog } from '../copal/overlays.js';
// The Session owner issues this witness; it is not derived from a resource ref.
export function validateFeedbackScope(value) {
  const common=['version','actorID','executionRevision','mapRevision','mappingRevision','workerGeneration','holderID'];
  const keys=value?.version===1?common:[...common,'actorSessionID','rootSessionID'];
  if (!value || typeof value!=='object' || Array.isArray(value) || ![1,2].includes(value.version)
      || Object.keys(value).length!==keys.length || keys.some(key=>!Object.hasOwn(value,key))
      || typeof value.actorID!=='string' || !value.actorID || new TextEncoder().encode(value.actorID).length>200
      || (value.version===1 && value.actorID!=='main')
      || ['executionRevision','mapRevision','mappingRevision','workerGeneration'].some(key=>!Number.isSafeInteger(value[key]) || value[key]<0)
      || typeof value.holderID!=='string' || !value.holderID || new TextEncoder().encode(value.holderID).length>256
      || (value.version===2 && ['actorSessionID','rootSessionID'].some(key=>typeof value[key]!=='string' || !value[key] || new TextEncoder().encode(value[key]).length>256)))
    throw new Error('Original task scope is unavailable; choose the target again.');
  return Object.freeze(Object.fromEntries(keys.map(key=>[key,value[key]])));
}
export async function captureFeedbackScope(chat, isCurrent, fetcher = fetch, selection = null) {
  if (!chat || typeof isCurrent !== 'function' || !isCurrent()) throw new Error('The Editor target changed; choose it again.');
  const query=selection ? `&actor=${encodeURIComponent(selection.actorID)}&actor_session=${encodeURIComponent(selection.sessionID)}` : '';
  const response = await fetcher(`/api/session/${encodeURIComponent(chat)}/plan/feedback?scope=1${query}`, {credentials:'same-origin',cache:'no-store'});
  const payload = await response.json();
  if (!isCurrent()) throw new Error('The Editor target changed; choose it again.');
  if (!response.ok) throw new Error('Original task scope is unavailable; choose the target again.');
  return validateFeedbackScope(payload?.scope);
}

/** Explicit user choice; the Session owner still authenticates the actor scope. */
export async function chooseFeedbackActor(chat, isCurrent, fetcher = fetch) {
  if (!chat || typeof isCurrent !== 'function' || !isCurrent()) throw Error('The Editor target changed.');
  const response=await fetcher(`/api/orchestration/${encodeURIComponent(chat)}`,{credentials:'same-origin',cache:'no-store'});
  const overview=await response.json();
  if (!isCurrent() || !response.ok || !Array.isArray(overview.actors) || typeof overview.engineSessionId!=='string') throw Error('Running actors are unavailable.');
  const actors=overview.actors.filter(actor=>actor?.status==='running' && typeof actor.actorID==='string' && typeof actor.sessionID==='string' && actor.hostContext?.chatID===chat && (actor.sessionID===overview.engineSessionId || (actor.sessionBinding?.kind==='peer_child' && actor.sessionBinding.parentSessionId===overview.engineSessionId))).slice(0,100);
  if (!actors.length) throw Error('No running actor is available.');
  return await new Promise(resolve=>{
    const dialog=document.createElement('dialog'),title=document.createElement('h2'),label=document.createElement('label'),select=document.createElement('select'),cancel=document.createElement('button'),choose=document.createElement('button'),status=document.createElement('p');
    dialog.className='copal-dialog';title.textContent='Choose a feedback actor';label.textContent='Running actor ';select.setAttribute('aria-label','Running actor');status.setAttribute('role','status');
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Choose an actor';select.append(placeholder);
    actors.forEach((actor,index)=>{const option=document.createElement('option');option.value=String(index);option.textContent=`${actor.actorID} · ${actor.sessionID===overview.engineSessionId?'root':'direct child'}`;select.append(option);});select.value='';label.append(select);
    cancel.type=choose.type='button';cancel.textContent='Cancel';choose.textContent='Use actor';let selected=null;
    cancel.addEventListener('click',()=>dialog.close());choose.addEventListener('click',()=>{const actor=select.value===''?null:actors[Number(select.value)];if(!actor || !isCurrent()){status.textContent='The actor or source is unavailable; choose again.';return;}selected={actorID:actor.actorID,sessionID:actor.sessionID};dialog.close();});
    dialog.addEventListener('close',()=>{dialog.remove();resolve(selected);},{once:true});dialog.append(title,label,status,cancel,choose);wireDialog(dialog);document.body.append(dialog);dialog.showModal();select.focus();
  });
}
