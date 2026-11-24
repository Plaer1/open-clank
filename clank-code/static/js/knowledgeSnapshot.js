import { wireDialog } from './copal/overlays.js';
import { buildBundle, parseBundle, importBundle, checkBundleItemStatus, sha256Hex, MAX_ITEMS, MAX_BUNDLE_BYTES } from './knowledgeSnapshotBundle.js';
const node=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};

export function openKnowledgeSnapshotExport({curatedID,current}) {
  if(typeof curatedID!=='string'||!curatedID||typeof current!=='function'||current()!==true)return false;
  const dialog=node('dialog');dialog.className='copal-dialog';const title=node('h3','Export one knowledge snapshot'),notice=node('p','Review the full authored text and destination category. Entity IDs, source relationships, subtype and question metadata are excluded; authored text may contain secrets.'),view=node('pre'),status=node('p'),preview=node('button','Preview'),download=node('button','Confirm and download'),close=node('button','Close');
  view.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:40vh;overflow:auto';for(const b of [preview,download,close]){b.type='button';b.className='copal-btn';}dialog.append(title,notice,view,status,preview,download,close);document.body.append(dialog);wireDialog(dialog);dialog.showModal();
  let active=true,busy=false,review=null;const abort=new AbortController();
  const valid=()=>active&&dialog.open&&dialog.isConnected&&current()===true;
  const controls=()=>{preview.disabled=busy||!valid();download.disabled=busy||!valid()||!review;};
  const post=(phase,body)=>fetch('/api/memory/knowledge-snapshot/export/'+phase,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:abort.signal});
  preview.addEventListener('click',async()=>{if(!valid()||busy)return;busy=true;controls();try{const r=await post('preview',{curated_id:curatedID});if(!r.ok)throw Error();const v=await r.json();if(!valid())return;if(!v?.expected||! /^[a-f0-9]{64}$/.test(v.review_sha256||'')||! /^[a-f0-9]{64}$/.test(v.package_sha256||'')||!v.package?.knowledge)throw Error();review=v;view.textContent=JSON.stringify(v.package,null,2);status.textContent='Review this exact text and category before confirming.';}catch{if(valid())status.textContent='Snapshot preview unavailable or changed.';}finally{busy=false;controls();}});
  download.addEventListener('click',async()=>{if(!valid()||busy||!review)return;busy=true;controls();try{const original=review,r=await post('confirm',{curated_id:curatedID,expected:original.expected,review_sha256:original.review_sha256});if(!r.ok)throw Error();const bytes=await r.arrayBuffer();if(!valid()||review!==original)return;const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');if(!valid()||hash!==original.package_sha256)throw Error();const url=URL.createObjectURL(new Blob([bytes],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='knowledge-snapshot.json';a.click();URL.revokeObjectURL(url);status.textContent='Snapshot downloaded.';}catch{if(valid())status.textContent='Snapshot changed or is unavailable.';}finally{busy=false;controls();}});
  const retire=()=>{if(dialog.open)dialog.close();};for(const event of ['workspace-change','openclank:auth-context-changed','openclank-account-changed','pagehide']){window.addEventListener(event,retire,{signal:abort.signal});document.addEventListener(event,retire,{signal:abort.signal});}close.addEventListener('click',retire);dialog.addEventListener('close',()=>{active=false;review=null;view.textContent='';abort.abort();dialog.remove();},{once:true});controls();return true;
}

export function openKnowledgeSnapshotImport({current}) {
  if(typeof current!=='function'||current()!==true)return false;
  const dialog=node('dialog');dialog.className='copal-dialog';const title=node('h3','Import one knowledge snapshot'),notice=node('p','Choose a snapshot file, then review its full text and destination category. Nothing is imported until you create the review draft.'),file=node('input'),view=node('pre'),meta=node('p'),status=node('p'),preview=node('button','Preview'),create=node('button','Create review draft'),check=node('button','Check status'),close=node('button','Close');
  file.type='file';file.accept='.json,application/json';check.hidden=true;
  view.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:40vh;overflow:auto';for(const b of [preview,create,check,close]){b.type='button';b.className='copal-btn';}dialog.append(title,notice,file,view,meta,status,preview,create,check,close);document.body.append(dialog);wireDialog(dialog);dialog.showModal();
  let active=true,busy=false,review=null,b64=null;const abort=new AbortController(),actionID=crypto.randomUUID();
  const valid=()=>active&&dialog.open&&dialog.isConnected&&current()===true;
  const controls=()=>{file.disabled=busy||!valid();preview.disabled=busy||!valid()||!b64;create.disabled=busy||!valid()||!review;check.disabled=busy||!valid();};
  const post=(phase,body)=>fetch('/api/memory/knowledge-snapshot/import/'+phase,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_id:actionID,package_b64:b64,...body}),signal:abort.signal});
  const reset=()=>{review=null;view.textContent='';meta.textContent='';check.hidden=true;};
  file.addEventListener('change',async()=>{if(!valid()||busy)return;reset();b64=null;status.textContent='';const f=file.files&&file.files[0];if(!f){controls();return;}if(f.size<1||f.size>65536){status.textContent='Snapshot file must be 1 to 65536 bytes.';controls();return;}busy=true;controls();try{const bytes=new Uint8Array(await f.arrayBuffer());if(!valid())return;let bin='';for(let i=0;i<bytes.length;i+=8192)bin+=String.fromCharCode(...bytes.subarray(i,i+8192));b64=btoa(bin);}catch{if(valid())status.textContent='Snapshot file could not be read.';}finally{busy=false;controls();}});
  preview.addEventListener('click',async()=>{if(!valid()||busy||!b64)return;busy=true;reset();controls();try{const r=await post('preview',{});if(!r.ok)throw Error();const v=await r.json();if(!valid())return;if(typeof v?.text!=='string'||typeof v.category!=='string'||! /^[a-f0-9]{64}$/.test(v.review_sha256||''))throw Error();review=v;view.textContent=v.text;meta.textContent='Category: '+v.category+'. Origin is unverified.'+(typeof v.origin==='string'&&v.origin?' Origin: '+v.origin:'');status.textContent='Review this exact text and category before creating the draft.';}catch{if(valid())status.textContent='Snapshot is invalid or unavailable.';}finally{busy=false;controls();}});
  const settle=async(phase)=>{if(!valid()||busy||!review)return;busy=true;controls();try{const original=review,r=await post(phase,{review_sha256:original.review_sha256});if(!r.ok)throw Error();const v=await r.json();if(!valid()||review!==original)return;if(v?.state==='staged'){check.hidden=true;status.textContent='A review-only draft was created. Edit it from the normal review list.';}else if(v?.state==='unknown'){check.hidden=false;status.textContent='The outcome is not yet known. Use Check status; do not repeat the import.';}else{check.hidden=true;status.textContent='The snapshot was not imported.';}}catch{if(valid()){check.hidden=false;status.textContent=phase==='confirm'?'The outcome is not known. Use Check status; do not repeat the import.':'Status is unavailable right now. Try Check status again.';}}finally{busy=false;controls();}};
  create.addEventListener('click',()=>settle('confirm'));check.addEventListener('click',()=>settle('status'));
  const retire=()=>{if(dialog.open)dialog.close();};for(const event of ['workspace-change','openclank:auth-context-changed','openclank-account-changed','pagehide']){window.addEventListener(event,retire,{signal:abort.signal});document.addEventListener(event,retire,{signal:abort.signal});}close.addEventListener('click',retire);dialog.addEventListener('close',()=>{active=false;review=null;b64=null;view.textContent='';abort.abort();dialog.remove();},{once:true});controls();return true;
}

// ---- Bundle dialogs (several snapshots in one file) ----

const SINGLE_SCHEMA='openclank.knowledge-snapshot.v1',PREFERENCE_LINE='This becomes a review draft. It is not standing guidance until you approve it in the review list.';
const STATE_WORDS={staged:'Review draft created.',unknown:'Outcome not known. Use Check status; do not repeat the import.',not_imported:'Not imported.',skipped:'Skipped.',invalid:'Preview failed or the snapshot is invalid.'};

export function exportSelectionProblem(ids){
  if(!Array.isArray(ids)||ids.length<1||ids.some(id=>typeof id!=='string'||!id))return 'Select at least one memory to export.';
  if(ids.length>MAX_ITEMS)return 'Too many selected: '+ids.length+'. A bundle holds at most '+MAX_ITEMS+' snapshots. Select fewer and try again.';
  if(new Set(ids).size!==ids.length)return 'The selection contains the same memory twice.';
  return null;
}
export const bundleStateLine=(result)=>'Item '+(result.index+1)+': '+(STATE_WORDS[result.state]||STATE_WORDS.invalid);
export function countBundleStates(results){
  const c={};for(const r of results)c[r.state]=(c[r.state]||0)+1;
  const parts=Object.keys(STATE_WORDS).filter(k=>c[k]).map(k=>c[k]+' '+k.replace('_',' '));
  return results.length+' items: '+(parts.join(', ')||'none processed')+'.';
}
// Null unless the bytes are a single-snapshot file (parsed by its schema field).
export function wrongDialogMessage(bytes){
  try{if(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes))?.schema===SINGLE_SCHEMA)return 'This file holds one snapshot. Use "Import snapshot…" instead.';}catch{}
  return null;
}
const bundleBlocked=(size)=>size<1||size>MAX_BUNDLE_BYTES?'Bundle file must be 1 to '+MAX_BUNDLE_BYTES+' bytes.':null;

function bundleShell(titleText,noticeText,current){
  const dialog=node('dialog');dialog.className='copal-dialog';
  const parts={dialog,title:node('h3',titleText),notice:node('p',noticeText),view:node('pre'),meta:node('p'),list:node('ul'),status:node('p')};
  parts.view.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:40vh;overflow:auto';
  const button=(label)=>{const b=node('button',label);b.type='button';b.className='copal-btn';return b;};
  const abort=new AbortController(),state={active:true};
  const valid=()=>state.active&&dialog.open&&dialog.isConnected&&current()===true;
  const retire=()=>{if(dialog.open)dialog.close();};
  const finish=(onClose)=>{for(const event of ['workspace-change','openclank:auth-context-changed','openclank-account-changed','pagehide']){window.addEventListener(event,retire,{signal:abort.signal});document.addEventListener(event,retire,{signal:abort.signal});}
    dialog.addEventListener('close',()=>{state.active=false;onClose();parts.view.textContent='';abort.abort();dialog.remove();},{once:true});};
  return {...parts,abort,valid,retire,button,finish};
}
const addLine=(list,text)=>{const li=node('li',text);list.append(li);return li;};

export function openKnowledgeSnapshotBundleExport({curatedIDs,current}) {
  if(!Array.isArray(curatedIDs)||typeof current!=='function'||current()!==true)return false;
  const problem=exportSelectionProblem(curatedIDs);if(problem&&curatedIDs.length<=MAX_ITEMS)return false;
  const s=bundleShell('Export knowledge snapshots','Each selected memory is previewed in turn. Review its full authored text and category, then choose Include or Skip for that item. Nothing is included without your choice.',current);
  const start=s.button('Start review'),include=s.button('Include'),skip=s.button('Skip'),download=s.button('Download knowledge-snapshots.json'),close=s.button('Close');
  s.dialog.append(s.title,s.notice,s.view,s.meta,s.status,s.list,start,include,skip,download,close);document.body.append(s.dialog);wireDialog(s.dialog);s.dialog.showModal();
  let busy=false,waiting=null,done=false;const confirmed=[];
  const controls=()=>{const ok=s.valid();start.disabled=busy||done||!ok||!!problem;include.disabled=skip.disabled=!waiting||!ok;download.disabled=busy||!ok||!confirmed.length||!done;};
  const post=(phase,body)=>fetch('/api/memory/knowledge-snapshot/export/'+phase,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:s.abort.signal});
  const decide=()=>new Promise(resolve=>{waiting=resolve;controls();});
  const answer=(v)=>{const w=waiting;waiting=null;if(w)w(v);controls();};
  include.addEventListener('click',()=>answer(true));skip.addEventListener('click',()=>answer(false));
  if(problem)s.status.textContent=problem;
  start.addEventListener('click',async()=>{if(!s.valid()||busy||done||problem)return;busy=true;controls();const out=[];
    try{for(let i=0;i<curatedIDs.length&&s.valid();i++){const id=curatedIDs[i],label='Item '+(i+1)+' of '+curatedIDs.length;let v=null;
      try{const r=await post('preview',{curated_id:id});if(!r.ok)throw Error();v=await r.json();if(!v?.expected||! /^[a-f0-9]{64}$/.test(v.review_sha256||'')||! /^[a-f0-9]{64}$/.test(v.package_sha256||'')||!v.package?.knowledge)throw Error();}catch{if(!s.valid())break;out.push(label+': not included (preview unavailable or changed).');continue;}
      if(!s.valid())break;s.view.textContent=JSON.stringify(v.package,null,2);s.meta.textContent=label+'. Category: '+v.package.knowledge.category+'.'+(v.package.knowledge.category==='preference'?' '+PREFERENCE_LINE:'');s.status.textContent='Review this exact text and category, then Include or Skip.';
      if(!await decide()){out.push(label+': not included (skipped).');continue;}
      try{const r=await post('confirm',{curated_id:id,expected:v.expected,review_sha256:v.review_sha256});if(!r.ok)throw Error();const bytes=new Uint8Array(await r.arrayBuffer());if(!s.valid())break;if(await sha256Hex(bytes)!==v.package_sha256)throw Error();confirmed.push(bytes);}catch{if(!s.valid())break;out.push(label+': not included (changed or unavailable).');}}
      if(s.valid()){done=true;s.view.textContent='';s.meta.textContent='';for(const t of out)addLine(s.list,t);
        s.status.textContent=confirmed.length+' of '+curatedIDs.length+' included.'+(confirmed.length?' Warning: the file contains the full authored text of every included item and may contain secrets. Store and share it accordingly.':'');}
    }finally{busy=false;controls();}});
  download.addEventListener('click',async()=>{if(!s.valid()||busy||!done||!confirmed.length)return;busy=true;controls();try{const bytes=await buildBundle(confirmed);if(!s.valid())return;const url=URL.createObjectURL(new Blob([bytes],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='knowledge-snapshots.json';a.click();URL.revokeObjectURL(url);s.status.textContent='Bundle downloaded.';}catch{if(s.valid())s.status.textContent='The bundle could not be built.';}finally{busy=false;controls();}});
  s.finish(()=>{confirmed.length=0;answer(false);});close.addEventListener('click',s.retire);controls();return true;
}

export function openKnowledgeSnapshotBundleImport({current}) {
  if(typeof current!=='function'||current()!==true)return false;
  const s=bundleShell('Import knowledge snapshots','Choose a bundle file. You review each snapshot\'s full text and category in turn; nothing is imported until you choose Create review draft for that item.',current);
  const file=node('input'),start=s.button('Start import'),create=s.button('Create review draft'),skip=s.button('Skip'),close=s.button('Close');
  file.type='file';file.accept='.json,application/json';
  s.dialog.append(s.title,s.notice,file,s.view,s.meta,s.status,s.list,start,create,skip,close);document.body.append(s.dialog);wireDialog(s.dialog);s.dialog.showModal();
  let busy=false,waiting=null,items=null;const checks=[];
  const controls=()=>{const ok=s.valid();file.disabled=busy||!ok;start.disabled=busy||!ok||!items;create.disabled=skip.disabled=!waiting||!ok;for(const b of checks)b.disabled=busy||!ok;};
  const post=(phase,body)=>fetch('/api/memory/knowledge-snapshot/import/'+phase,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:s.abort.signal});
  const answer=(v)=>{const w=waiting;waiting=null;if(w)w(v);controls();};
  create.addEventListener('click',()=>answer(true));skip.addEventListener('click',()=>answer(false));
  file.addEventListener('change',async()=>{if(!s.valid()||busy)return;items=null;s.status.textContent='';const f=file.files&&file.files[0];if(!f){controls();return;}
    const blocked=bundleBlocked(f.size);if(blocked){s.status.textContent=blocked;controls();return;}
    busy=true;controls();try{const bytes=new Uint8Array(await f.arrayBuffer());if(!s.valid())return;try{items=(await parseBundle(bytes)).items;s.status.textContent=items.length+' snapshots found. Press Start import.';}catch{s.status.textContent=wrongDialogMessage(bytes)||'This is not a valid snapshot bundle.';}}catch{if(s.valid())s.status.textContent='Bundle file could not be read.';}finally{busy=false;controls();}});
  const approve=(item,preview)=>new Promise(resolve=>{s.view.textContent=preview.text;s.meta.textContent='Item '+(item.index+1)+' of '+items.length+'. Category: '+preview.category+'. Origin is unverified.';
    if(preview.category==='preference')s.meta.textContent+=' '+PREFERENCE_LINE;s.status.textContent='Review this exact text and category, then create the draft or skip.';waiting=resolve;controls();});
  start.addEventListener('click',async()=>{if(!s.valid()||busy||!items)return;busy=true;const run=items;items=null;file.value='';controls();s.list.textContent='';checks.length=0;
    try{const results=await importBundle({items:run,post,newActionId:()=>crypto.randomUUID(),current:()=>s.valid(),approve,onItem:(r)=>{s.status.textContent='Processed '+(r.index+1)+' of '+run.length+'.';}});
      if(!s.valid())return;s.view.textContent='';s.meta.textContent='';s.status.textContent=countBundleStates(results);
      for(const r of results){const li=addLine(s.list,bundleStateLine(r));if(r.state!=='unknown')continue;const b=s.button('Check status');li.append(' ',b);checks.push(b);
        b.addEventListener('click',async()=>{if(!s.valid()||busy)return;busy=true;controls();try{const c=await checkBundleItemStatus({item:run[r.index],post,actionId:r.actionId,reviewSha256:r.reviewSha256});if(!s.valid())return;r.state=c.state;li.firstChild.textContent=bundleStateLine(r);s.status.textContent=countBundleStates(results);if(c.state!=='unknown'){b.remove();checks.splice(checks.indexOf(b),1);}}finally{busy=false;controls();}});}
    }finally{busy=false;controls();}});
  s.finish(()=>{items=null;answer(false);});close.addEventListener('click',s.retire);controls();return true;
}
