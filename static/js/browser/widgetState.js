// Source declaration is supplied by WB06, never by the isolated frame.
const channel = 'openclank.widget-state.v1';
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).length;
const id = /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/;
const sha = /^sha256:[a-f0-9]{64}$/;
const plain = value => value && typeof value === 'object' && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
function schemaOf(value, digest) {
  const schema=value?.schema;
  if (!plain(value) || Object.keys(value).sort().join() !== 'declaration_digest,schema,source_digest' || !sha.test(value.declaration_digest) || value.source_digest !== digest || !plain(schema) || Object.keys(schema).sort().join() !== 'fields,version,widgetId' || schema.version !== 1 || !id.test(schema.widgetId) || !Array.isArray(schema.fields) || !schema.fields.length || schema.fields.length > 32 || bytes(value)>16384) throw Error('Widget declaration unavailable.');
  const seen=new Set();
  for (const f of schema.fields) {
    if (!plain(f) || !id.test(f.id) || seen.has(f.id) || typeof f.label !== 'string' || !f.label.length || f.label.length>128 || /[\x00-\x1f]/.test(f.label)) throw Error('Widget declaration unavailable.'); seen.add(f.id);
    const keys=Object.keys(f).sort().join();
    if (f.type==='boolean' && keys==='id,label,type') continue;
    if (f.type==='number' && keys==='id,label,max,min,type' && Number.isFinite(f.min) && Number.isFinite(f.max) && Math.abs(f.min)<=1e9 && Math.abs(f.max)<=1e9 && f.min<=f.max) continue;
    if (f.type==='text' && keys==='id,label,maxLength,type' && Number.isSafeInteger(f.maxLength) && f.maxLength>=1 && f.maxLength<=1024) continue;
    if (f.type==='enum' && keys==='choices,id,label,type' && Array.isArray(f.choices) && f.choices.length>0 && f.choices.length<=32 && f.choices.every(v=>typeof v==='string' && v.length>0 && v.length<=128) && new Set(f.choices).size===f.choices.length) continue;
    throw Error('Widget declaration unavailable.');
  }
  return schema;
}
function checkedValues(schema, rows, requireAll=false) {
  if (!Array.isArray(rows) || !rows.length || rows.length>schema.fields.length || bytes(rows)>16384) throw Error('Widget values unavailable.');
  const fields=new Map(schema.fields.map(f=>[f.id,f])), seen=new Set();
  const result=rows.map(row=>{
    if (!plain(row) || Object.keys(row).sort().join()!=='id,value' || !fields.has(row.id) || seen.has(row.id)) throw Error('Widget values do not match the declaration.');
    seen.add(row.id); const f=fields.get(row.id), v=row.value;
    if (!(f.type==='boolean' && typeof v==='boolean' || f.type==='number' && typeof v==='number' && Number.isFinite(v) && v>=f.min && v<=f.max || f.type==='text' && typeof v==='string' && v.length<=f.maxLength || f.type==='enum' && f.choices.includes(v))) throw Error('Widget value is invalid.');
    return {id:row.id,value:v};
  });
  if (requireAll && seen.size!==fields.size) throw Error('Widget response is incomplete.');
  return result;
}
export function createWidgetStateController({context, post}) {
  let pending=null;
  const reset=()=>{if(pending){clearTimeout(pending.timer);pending.reject(Error('Preview changed.'));pending=null;}};
  function declaration(){const c=context(); return schemaOf(c.session?.widget_state,c.session?.digest);}
  function request(type, values) {
    reset(); const c=context(), session=c.session, schema=declaration();
    if (!c.active || !c.frameWindow || !session) throw Error('Preview unavailable.');
    const checked=values ? checkedValues(schema,values) : null;
    const nonce=[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');
    return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{if(pending?.nonce===nonce){pending=null;reject(Error('Widget did not respond.'));}},5000);pending={nonce,timer,resolve,reject,session:session.session_id,generation:c.generation,type}; if(!post({channel,type,nonce,session_id:session.session_id,generation:c.generation,source_digest:session.digest,declaration_digest:session.widget_state.declaration_digest,...(checked?{values:checked}:{})})){reset();}});
  }
  function receive(event){
    const c=context(), p=pending, d=event.data;
    if(!p || !c.active || !c.frameWindow || event.source!==c.frameWindow || event.origin!==c.origin || c.session?.session_id!==p.session || c.generation!==p.generation || !plain(d) || d.channel!==channel || d.session_id!==p.session || d.generation!==p.generation || d.nonce!==p.nonce || d.type!==(p.type==='capture'?'captured':'applied') || d.source_digest!==c.session.digest || d.declaration_digest!==c.session.widget_state?.declaration_digest) return;
    try {const keys=p.type==='capture'?'channel,declaration_digest,generation,nonce,session_id,source_digest,type,values':'accepted,channel,declaration_digest,generation,nonce,session_id,source_digest,type'; if(Object.keys(d).sort().join()!==keys || (p.type==='apply' && d.accepted!==true)) throw Error('Widget response is invalid.'); const result=p.type==='capture'?checkedValues(declaration(),d.values,true):true;clearTimeout(p.timer);pending=null;p.resolve(result);}catch(error){clearTimeout(p.timer);pending=null;p.reject(error);}
  }
  return Object.freeze({declaration,receive,reset,capture:()=>request('capture'),apply:values=>request('apply',values), envelope(values){const c=context();if(typeof c.session.resource_key!=='string'||!c.session.resource_key||typeof c.session.revision!=='string'||!c.session.revision)throw Error('Source identity unavailable.');return {version:2,resource_id:c.session.resource_key,revision:c.session.revision,widgetId:declaration().widgetId,source_digest:c.session.digest,declaration_digest:c.session.widget_state.declaration_digest,values:checkedValues(declaration(),values)};},readEnvelope(value){const c=context();if(!plain(value)||!((value.version===1&&Object.keys(value).sort().join()==='declaration_digest,source_digest,values,version,widgetId')||(value.version===2&&Object.keys(value).sort().join()==='declaration_digest,resource_id,revision,source_digest,values,version,widgetId'&&value.resource_id===c.session.resource_key&&value.revision===c.session.revision))||value.widgetId!==declaration().widgetId||value.source_digest!==c.session.digest||value.declaration_digest!==c.session.widget_state.declaration_digest||bytes(value)>16384)throw Error('Backup source or declaration differs.');return checkedValues(declaration(),value.values);}});
}

// Host-owned review UI; runtime claims never become verified source facts.
export function openWidgetStateDialog({widget, isCurrent, save, feedback, loadFiles}) {
  const dialog=document.createElement('dialog'), heading=document.createElement('h2'), info=document.createElement('p'), list=document.createElement('div'), status=document.createElement('p');
  heading.textContent='Widget state'; info.textContent='Values are reported by the widget and are unverified. Select only what you want to save, apply, or include in feedback.'; status.setAttribute('role','status');
  const button=text=>{const b=document.createElement('button');b.type='button';b.textContent=text;return b;};
  const capture=button('Capture current state'), load=button('Load legacy file…'), loadManaged=button('Load from Files…'), apply=button('Apply selected'), store=button('Save selected to Files…'), send=button('Use selected in feedback…'), close=button('Close');
  const file=document.createElement('input');file.type='file';file.accept='application/json,.json';file.hidden=true;let values=[], fields=[], epoch=0, busy=false, verifyCopy=null, legacy=false;
  const alive=()=>dialog.isConnected && isCurrent();
  const render=()=>{list.replaceChildren(); for(const row of values){const field=fields.find(f=>f.id===row.id), label=document.createElement('label'), box=document.createElement('input'), text=document.createElement('span');box.type='checkbox';box.dataset.id=row.id;box.addEventListener('change',()=>{epoch++;});text.textContent=` ${field?.label || row.id}: ${JSON.stringify(row.value)} `;label.append(box,text);list.append(label);}};
  const selected=()=>{const ids=new Set([...list.querySelectorAll('input:checked')].map(x=>x.dataset.id));const rows=values.filter(v=>ids.has(v.id));if(!rows.length)throw Error('Select at least one field.');return rows;};
  const run=async(fn)=>{if(busy || !alive())return;busy=true;for(const box of list.querySelectorAll('input'))box.disabled=true;const ticket=epoch;try{await fn(ticket);if(!alive())throw Error('Preview changed.');}catch(error){if(dialog.isConnected)status.textContent=error?.message || 'Widget state unavailable.';}finally{busy=false;for(const box of list.querySelectorAll('input'))box.disabled=false;}};
  capture.onclick=()=>run(async()=>{await widget.verify();const got=await widget.capture();if(!alive())throw Error('Preview changed.');fields=widget.declaration().fields;values=got;verifyCopy=null;legacy=false;epoch++;render();status.textContent='Widget reported these values; they are unverified.';});
  load.onclick=()=>file.click(); file.onchange=()=>{const chosen=file.files?.[0];file.value='';if(!chosen)return;void run(async()=>{if(chosen.size<1 || chosen.size>16384)throw Error('State copy is too large.');await widget.verify();const raw=await chosen.text();if(!alive() || new TextEncoder().encode(raw).length>16384)throw Error('Preview changed or copy is too large.');const parsed=JSON.parse(raw);values=widget.readEnvelope(parsed);fields=widget.declaration().fields;verifyCopy=null;legacy=parsed.version===1;epoch++;render();status.textContent=parsed.version===1?'Legacy copy loaded for review; its resource identity is unbound.':'State copy loaded for review; choose fields before applying.';});};
  loadManaged.onclick=()=>run(async()=>{await widget.verify();const copy=await loadFiles();if(!copy)return;if(!alive())throw Error('Preview changed.');const parsed=JSON.parse(copy.text);if(parsed.version!==2)throw Error('Select a resource-bound state copy.');values=widget.readEnvelope(parsed);fields=widget.declaration().fields;verifyCopy=copy.verify;legacy=false;epoch++;render();status.textContent='Files state copy loaded for review; choose fields before applying.';});
  apply.onclick=()=>run(async ticket=>{const rows=selected();if(verifyCopy)await verifyCopy();if(!alive()||ticket!==epoch)throw Error('Selection changed.');await widget.verify();if(!alive()||ticket!==epoch)throw Error('Selection changed.');await widget.apply(rows);if(ticket!==epoch)throw Error('Selection changed.');status.textContent='Widget acknowledged the selected values; application is unverified.';});
  store.onclick=()=>run(async ticket=>{if(legacy)throw Error('Capture current widget state before saving a bound copy.');const rows=selected(), envelope=widget.envelope(rows);await widget.verify();if(ticket!==epoch||!alive())throw Error('Selection changed.');const result=await save(new File([JSON.stringify(envelope)],'widget-state.json',{type:'application/json'}),()=>alive()&&ticket===epoch);if(alive())status.textContent=result?.recoveryUnavailable?'Saved; recovery copy unavailable.':'Files save completed.';});
  send.onclick=()=>run(async ticket=>{const rows=selected();await widget.verify();if(ticket!==epoch||!alive())throw Error('Selection changed.');await feedback(rows,fields,()=>alive()&&ticket===epoch);if(alive())status.textContent='Selected values added as a visible feedback draft; review before saving.';});
  close.onclick=()=>{if(!busy)dialog.close();};dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});dialog.addEventListener('close',()=>dialog.remove());
  dialog.append(heading,info,capture,load,loadManaged,file,list,apply,store,send,close,status);document.body.append(dialog);dialog.showModal();return dialog;
}

// One UI-owner receipt slot. A thrown call after dispatch is never replayed.
export async function saveWidgetCopy({state, binding, isCurrent, verify, file, importFile}) {
  if (state.pending) throw Error('Check the previous widget Files receipt first.');
  let dispatched=false, result;
  try { result=await importFile(file,{purpose:'widget-state',isCurrent,beforeDispatch:async()=>{if (!isCurrent() || await verify() !== true || !isCurrent()) return false; dispatched=true;return true;}}); }
  catch (error) { if (dispatched) state.pending={...binding,unknown:true}; throw error; }
  if (result?.receiptHandles?.length) state.pending={...binding,receiptHandles:result.receiptHandles};
  else if (result?.submitted && !result?.committed && !result?.rejected) state.pending={...binding,unknown:true};
  if (state.pending) throw Error('Files receipt is pending or unknown; check its status before another save.');
  if (!result?.committed) throw Error(result?.cancelled ? 'Files save cancelled.' : 'Files save did not commit.');
  return result;
}
export async function checkWidgetCopy({state,account,workspace,isCurrent,reconcile}) {
  const pending=state.pending; if (!pending) return {state:'none'};
  if (pending.account!==account || pending.workspace!==workspace || !isCurrent()) return {state:'unavailable'};
  if (pending.unknown || !pending.receiptHandles?.length) return {state:'unknown'};
  const result=await reconcile(pending.receiptHandles,{isCurrent});
  if (!isCurrent() || state.pending!==pending) return {state:'unavailable'};
  if (!result?.terminal) { pending.receiptHandles=result?.receiptHandles || pending.receiptHandles; return {state:'pending'}; }
  state.pending=null; return {state:result.committed?(result.recoveryUnavailable?'saved_recovery_unavailable':'saved'):'rejected'};
}
export function createWidgetFeedbackGuard(base,selection) {
  let accepted=false;return Object.freeze({current:()=>Boolean(base() && (accepted || selection())),accept:()=>{if (!base() || !selection()) return false;accepted=true;return true;}});
}
