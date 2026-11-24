import { wireDialog } from './copal/overlays.js';
import Storage from './storage.js';

const account=()=>{const a=window.chatModule?.ratingAccountScope?.();return a?.owner&&document.body?.dataset?.accountId===a.owner?{owner:a.owner,epoch:a.authEpoch}:null;};
const workspace=()=>Storage.get(Storage.KEYS.WORKSPACE_ID,'')||'default';
const pendingKey=owner=>`skills:snapshot:pending:${encodeURIComponent(owner)}`;
const el=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};

export function openSkillSnapshot({skillID=null,isSelectionCurrent=()=>true,onComplete=()=>{}}={}) {
  const scope=account(), originalWorkspace=workspace(), exporting=typeof skillID==='string'&&!!skillID;
  if(!scope||typeof crypto?.randomUUID!=='function')return false;
  const dialog=el('dialog');dialog.className='copal-dialog';
  const title=el('h3',exporting?'Export published skill snapshot':'Import a skill snapshot');
  const info=el('p',exporting?'Review all authored content before downloading. Authored text may contain secrets.':'Review all content before creating an independent draft. Origin metadata is unverified.');
  const format=document.createElement('select'); format.setAttribute('aria-label','Import format'); format.hidden=exporting;
  for(const [value,label] of [['snapshot','OpenClank skill snapshot'],['external','External SKILL.md']]){const option=el('option',label);option.value=value;format.append(option);}
  const file=document.createElement('input');file.type='file';file.accept='.json,application/json';file.setAttribute('aria-label','Snapshot JSON package');file.hidden=exporting;
  const name=document.createElement('input');name.placeholder='Destination skill name';name.setAttribute('aria-label','Destination skill name');name.hidden=exporting;
  const view=el('pre');view.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:40vh;overflow:auto';
  const status=el('p'), preview=el('button','Preview'), issue=el('button',exporting?'Confirm and download':'Create draft'), check=el('button','Check original status'), close=el('button','Close');
  for(const b of [preview,issue,check,close]){b.type='button';b.className='copal-btn';}
  const footer=el('footer');footer.className='copal-dialog-actions';footer.append(preview,issue,check,close);dialog.append(title,info,format,file,name,view,status,footer);
  let disposed=false,busy=false,epoch=0,review=null,selected=null;const reads=new AbortController();
  const current=()=>{const a=account();return !disposed&&dialog.isConnected&&dialog.open&&a?.owner===scope.owner&&a.epoch===scope.epoch&&workspace()===originalWorkspace&&(!exporting||isSelectionCurrent()===true);};
  let pending;try{const raw=sessionStorage.getItem(pendingKey(scope.owner));pending=raw?JSON.parse(raw):null;if(pending&&(!pending.request_key||!pending.package_sha256||!pending.destination||pending.owner!==scope.owner))pending={invalid:true};}catch{pending={invalid:true};}
  const controls=()=>{preview.disabled=busy||!current();issue.disabled=busy||!current()||!review||(!exporting&&!!pending);check.hidden=exporting;check.disabled=busy||!current()||!pending||pending.invalid;};
  const valid=t=>current()&&t===epoch;
  const api=async(action,body)=>{const r=await fetch(`/api/skills/snapshot/${action}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:reads.signal});if(!r.ok)throw Error('Snapshot is unavailable or changed.');return r.json();};
  const run=async f=>{if(busy||!current())return;busy=true;const t=epoch;controls();try{await f(t);}catch{if(valid(t))status.textContent='Snapshot is unavailable or changed.';}finally{busy=false;controls();}};
  const invalidate=()=>{epoch++;review=null;selected=null;view.textContent='';controls();};
  file.addEventListener('change',invalidate);name.addEventListener('input',invalidate);format.addEventListener('change',()=>{file.accept=format.value==='external'?'.md,text/markdown':'.json,application/json';invalidate();});
  preview.addEventListener('click',()=>run(async t=>{
    let v;
    if(exporting) v=await api('export-preview',{skill_id:skillID});
    else {
      const f=file.files?.[0];if(!f||f.size>(format.value==='external'?256*1024:4*1024*1024)||!name.value.trim())throw Error();
      const bytes=await f.arrayBuffer();const raw=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);if(!valid(t))return;
      if(format.value==='external') {
        v=await api('external-preview',{raw,destination_name:name.value.trim()});
        if(!valid(t)||v?.review?.format!=='external-skill-markdown'||!Array.isArray(v.review.unsupported)||! /^[a-f0-9]{64}$/.test(v.review.source_sha256||'')||! /^[a-f0-9]{64}$/.test(v.review.review_sha256||''))throw Error();
        selected=raw;review=structuredClone(v.review);view.textContent=JSON.stringify(review.unsupported.length?{review}:{review,source:raw},null,2);
        status.textContent=review.unsupported.length?'Unsupported metadata prevents import.':'Draft and review only. Native skill delivery of the original source is not yet available.';
        if(review.unsupported.length)review=null;
        return;
      }
      v=await api('import-preview',{package_json:raw,destination_name:name.value.trim()});
    }
    if(!valid(t))return;if(v?.review?.version!==1||! /^[a-f0-9]{64}$/.test(v.review.package_sha256||'')||! /^[a-f0-9]{64}$/.test(v.review.review_sha256||'')||!v.package||!Array.isArray(v.package.files))throw Error();
    selected=structuredClone(v.package);review=structuredClone(v.review);view.textContent=JSON.stringify({review,package:selected},null,2);status.textContent='Review the complete authored fields and companion contents.';
  }));
  issue.addEventListener('click',()=>run(async t=>{
    if(!review||!selected)return;const originalReview=review,originalPackage=selected;
    if(exporting){const v=await api('export',{skill_id:skillID,review_sha256:originalReview.review_sha256});if(!valid(t))return;if(v?.package_sha256!==originalReview.package_sha256||JSON.stringify(v.package)!==JSON.stringify(originalPackage))throw Error();const blob=new Blob([JSON.stringify(v.package,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`${v.package.skill.name}.json`;a.click();URL.revokeObjectURL(url);status.textContent='Snapshot downloaded.';return;}
    if(pending)return;const request_key=crypto.randomUUID(),destination=name.value.trim(),external=originalReview.format==='external-skill-markdown';
    const record={owner:scope.owner,request_key,package_sha256:external?originalReview.source_sha256:originalReview.package_sha256,destination,...(external?{profile:'external'}:{})};
    const encoded=JSON.stringify(record);sessionStorage.setItem(pendingKey(scope.owner),encoded);if(sessionStorage.getItem(pendingKey(scope.owner))!==encoded)throw Error();pending=record;review=null;controls();status.textContent='Creating the independent draft…';
    try{const v=external?await api('external-import',{raw:originalPackage,destination_name:destination,review_sha256:originalReview.review_sha256,request_key}):await api('import',{package:originalPackage,destination_name:destination,review_sha256:originalReview.review_sha256,request_key});
      if(!valid(t))return;if(v?.ok!==true||!v.skill)throw Error();sessionStorage.removeItem(pendingKey(scope.owner));pending=null;status.textContent='Draft created. Review it in Skills before publishing.';await onComplete(v.skill,()=>valid(t));
    }catch{if(valid(t))status.textContent='Outcome unknown. Check the original status; do not issue another import.';}
  }));
  check.addEventListener('click',()=>run(async t=>{const original=pending;if(!original||original.invalid)return;const suffix=original.profile==='external'?'&profile=external':'';const r=await fetch(`/api/skills/snapshot/status?request_key=${encodeURIComponent(original.request_key)}${suffix}`,{credentials:'same-origin',cache:'no-store',signal:reads.signal});if(!r.ok)throw Error();const v=await r.json();if(!valid(t)||pending!==original)return;if(v?.state==='complete'){sessionStorage.removeItem(pendingKey(scope.owner));pending=null;status.textContent='Original draft completed. Review it in Skills before publishing.';await onComplete(v,()=>valid(t));}else status.textContent='Original outcome remains unknown.';}));
  const retire=()=>{if(dialog.open)dialog.close();};for(const event of ['openclank-account-changed','openclank:auth-context-changed','workspace-change']){window.addEventListener(event,retire,{signal:reads.signal});document.addEventListener(event,retire,{signal:reads.signal});}
  close.addEventListener('click',retire);dialog.addEventListener('close',()=>{disposed=true;epoch++;reads.abort();},{once:true});wireDialog(dialog);document.body.append(dialog);dialog.showModal();if(pending)status.textContent='An original import is unresolved. Check its status before creating another draft.';controls();return true;
}
