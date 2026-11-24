import { wireDialog } from './copal/overlays.js';
import { createResourcePicker } from './copal/resourcePicker.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import Storage from './storage.js';

// One original issued operation is retained per owner; it is not an import grant.
const pendingKey = owner => `skills:marketplace:pending:${encodeURIComponent(owner)}`;
const workspace = () => Storage.get(Storage.KEYS.WORKSPACE_ID, '') || 'default';
const account = () => {
  const value = window.chatModule?.ratingAccountScope?.();
  return value?.owner && document.body?.dataset?.accountId === value.owner ? {owner:value.owner, epoch:value.authEpoch} : null;
};
const element = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };

export function openSkillMarketplace({onComplete = () => {}} = {}) {
  const scope = account(), originalWorkspace = workspace();
  if (!scope || typeof crypto?.randomUUID !== 'function') return false;
  const dialog = document.createElement('dialog'); dialog.className = 'copal-dialog';
  const title = element('h3','Import a skill from a marketplace');
  const description = element('p','Choose the source and review the selected skill before creating a draft.');
  const sourceKind = document.createElement('select');
  sourceKind.setAttribute('aria-label', 'Skill marketplace source');
  for (const [value, label] of [['github', 'Pinned GitHub catalog'], ['zcode', 'ZCode'],
    ['clawhub', 'OpenClaw / ClawHub'], ['hermes', 'Hermes Skills Hub'], ['files', 'Files folder']]) {
    const option = element('option', label);
    option.value = value;
    sourceKind.append(option);
  }
  const query = document.createElement('input');
  query.type = 'search';
  query.maxLength = 100;
  query.placeholder = 'Search marketplace';
  query.setAttribute('aria-label', 'Search marketplace');
  query.hidden = true;
  const warning = element('strong', 'WARNING THIS IS A THIRD PARTY TOOL NOT AUDITED BY THE OPEN CLANK DEVELOPERS');
  warning.hidden = true;
  const sourceURL = document.createElement('input'); sourceURL.type='url'; sourceURL.placeholder='Pinned public GitHub catalog URL'; sourceURL.setAttribute('aria-label','Pinned public GitHub catalog URL');
  const choose = element('button','Choose Files folder'), catalogButton = element('button','Preview catalog'), pluginButton = element('button','View components'), previewButton = element('button','Review skill'), create = element('button','Create draft'), check = element('button','Check original status'), close = element('button','Close');
  for (const button of [choose,catalogButton,pluginButton,previewButton,create,check,close]) { button.type='button'; button.className='copal-btn'; }
  const plugins=document.createElement('select'), skills=document.createElement('select'); plugins.setAttribute('aria-label','Plugin');skills.setAttribute('aria-label','Skill');
  const sourceLabel=element('p','No source selected.'), catalogInfo=element('pre'), componentInfo=element('pre'), reviewInfo=element('pre'), status=element('p');
  for(const node of [catalogInfo,componentInfo,reviewInfo]) {node.style.whiteSpace='pre-wrap';node.style.overflowWrap='anywhere';node.style.maxHeight='15rem';node.style.overflow='auto';}
  const actions=element('footer'); actions.className='copal-dialog-actions';actions.append(catalogButton,pluginButton,previewButton,create,check,close);
  choose.hidden = true;
  dialog.append(title,description,sourceKind,sourceURL,query,choose,sourceLabel,plugins,skills,warning,actions,status,catalogInfo,componentInfo,reviewInfo);
  let disposed=false, epoch=0, source=null, canonical=null, review=null, picker=null, sourceGeneration=null, busy=false;
  const reads=new AbortController();
  const current=()=>{const a=account();return !disposed && dialog.isConnected && dialog.open && !!a && a.owner===scope.owner && a.epoch===scope.epoch && workspace()===originalWorkspace;};
  const readPending=()=>{try {const v=JSON.parse(sessionStorage.getItem(pendingKey(scope.owner)) || 'null');return v?.owner===scope.owner && typeof v.request_key==='string' && v.body?.request_key===v.request_key ? v : null;}catch{return null;}};
  let pending=readPending();
  const controls=()=>{catalogButton.disabled=busy||!current();choose.disabled=busy||!current();pluginButton.disabled=busy||!canonical||!plugins.value||!current();previewButton.disabled=busy||!canonical||!plugins.value||!skills.value||!current();create.disabled=busy||!!pending||!review||!current();check.disabled=busy||!pending||!current();};
  const invalidate=()=>{epoch++;canonical=null;review=null;warning.hidden=true;sourceGeneration=null;plugins.replaceChildren();skills.replaceChildren();catalogInfo.textContent='';componentInfo.textContent='';reviewInfo.textContent='';controls();};
  const api=async(action,body,signal=reads.signal)=>{const response=await fetch(`/api/skills/marketplace/${action}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal});if(!response.ok)throw new Error('Marketplace source or review is unavailable or changed.');return response.json();};
  const guarded=async fn=>{if(!current()||busy)return;busy=true;const ticket=epoch;controls();try{await fn(ticket);}catch(error){if(current()&&ticket===epoch)status.textContent=String(error?.message || 'Marketplace unavailable.');}finally{busy=false;controls();}};
  const validTicket=ticket=>current()&&ticket===epoch;
  const ensureSource=async(ticket)=>{
    if(!validTicket(ticket))throw new Error('The original account or selection changed.');
    if(canonical?.kind==='files'){
      const roots=await filesFacadeClient.roots({copalWorkspace:originalWorkspace,signal:reads.signal});
      if(!validTicket(ticket)||Number(roots?.policy_generation ?? roots?.generation)!==sourceGeneration)throw new Error('Files access changed; select the folder again.');
    }
  };
  const options=(select,rows,placeholder)=>{select.replaceChildren();const empty=element('option',placeholder);empty.value='';select.append(empty);for(const row of rows){const option=element('option',row.label);option.value=row.value;select.append(option);}select.value='';};
  const registry = () => ['zcode','clawhub','hermes'].includes(sourceKind.value);
  sourceKind.addEventListener('change', () => {
    source = null;
    sourceURL.hidden = sourceKind.value !== 'github';
    choose.hidden = sourceKind.value !== 'files';
    query.hidden = !registry();
    sourceLabel.textContent = sourceKind.selectedOptions[0]?.textContent || '';
    invalidate();
  });
  query.addEventListener('input', invalidate);
  sourceURL.addEventListener('input',()=>{source=null;sourceLabel.textContent='Public catalog URL';invalidate();});
  choose.addEventListener('click',()=>guarded(async ticket=>{
    const roots=await filesFacadeClient.roots({copalWorkspace:originalWorkspace,signal:reads.signal});
    const generation=Number(roots?.policy_generation ?? roots?.generation);if(!validTicket(ticket)||!Number.isSafeInteger(generation)||generation<0)throw new Error('Files access is unavailable.');
    const selected=await new Promise(resolve=>{let item=null;picker=createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:scope.owner,workspaceScope:originalWorkspace,generation,signal:reads.signal,isOriginCurrent:()=>validTicket(ticket),rootLabel:'Choose the owned marketplace folder',isCompatible:row=>row.provider==='host'?'':'Choose a Host folder.',onSelect:row=>{item=row;return true;},onClose:cancelled=>{picker=null;resolve(cancelled?null:item);}});void picker.open();});
    if(!validTicket(ticket)||!selected)return;if(!selected.ref||!selected.revision||selected.generation!==generation)throw new Error('The selected folder changed.');
    source={kind:'files',workspace_id:originalWorkspace,resource_ref:selected.ref,expected_revision:structuredClone(selected.revision)};sourceURL.value='';sourceLabel.textContent=`Folder: ${selected.name || 'selected Host folder'}`;invalidate();sourceGeneration=generation;
  }));
  catalogButton.addEventListener('click',()=>guarded(async ticket=>{
    const selected = registry() ? {kind:'registry',registry:sourceKind.value,query:query.value.trim()}
      : source || {kind:'github',url:sourceURL.value.trim()};
    if (sourceKind.value === 'files' && !source) throw new Error('Choose a Files folder.');
    if (selected.kind==='github'&&!selected.url) throw new Error('Enter a pinned public catalog URL.');
    if(selected.kind==='files'){canonical=selected;await ensureSource(ticket);}
    const value=await api('catalog',{source:selected});if(!validTicket(ticket)||value?.result?.version!==1||!value.selector||!Array.isArray(value.result.plugins))throw new Error('Catalog unavailable.');
    canonical=structuredClone(value.selector);review=null;options(plugins,value.result.plugins.filter(p=>p?.supported===true&&typeof p.name==='string').map(p=>({value:p.name,label:[p.title || p.name,p.publisher,p.version,p.registry].filter(Boolean).join(' · ')})),'Choose a result');skills.replaceChildren();componentInfo.textContent='';reviewInfo.textContent='';catalogInfo.textContent=JSON.stringify(value.result,null,2);status.textContent='Catalog captured. Select a plugin to inspect its components.';
  }));
  plugins.addEventListener('change',()=>{
    epoch++;
    review=null;
    warning.hidden=true;
    if (canonical?.kind === 'registry') delete canonical.pin;
    skills.replaceChildren();
    componentInfo.textContent='';
    reviewInfo.textContent='';
    controls();
  });
  pluginButton.addEventListener('click',()=>guarded(async ticket=>{await ensureSource(ticket);const name=plugins.value,value=await api('components',{source:canonical,plugin_name:name});if(!validTicket(ticket)||value?.result?.plugin_name!==name||!Array.isArray(value.result.skills))throw new Error('Components unavailable.');canonical=structuredClone(value.selector);review=null;options(skills,value.result.skills.filter(x=>typeof x==='string').map(x=>({value:'skill:'+encodeURIComponent(x),label:x || 'Plugin root'})),'Choose a skill');componentInfo.textContent=JSON.stringify(value.result,null,2);reviewInfo.textContent='';status.textContent=value.result.blocked
    ? 'This release is blocked by its upstream registry.'
    : 'Review the selected skill and any publisher notices before creating a draft.';}));
  skills.addEventListener('change',()=>{epoch++;review=null;warning.hidden=true;reviewInfo.textContent='';controls();});
  previewButton.addEventListener('click',()=>guarded(async ticket=>{await ensureSource(ticket);const name=plugins.value,path=decodeURIComponent(skills.value.slice(6)),value=await api('preview',{source:canonical,plugin_name:name,skill_path:path});if(!validTicket(ticket)||value?.result?.version!==1||value.result.skill_path!==path||value.result.plugin?.name!==name||! /^[a-f0-9]{64}$/.test(value.result.review_sha256||'')||!Array.isArray(value.result.files))throw new Error('Review unavailable.');canonical=structuredClone(value.selector);review=Object.freeze({source:structuredClone(canonical),plugin_name:name,skill_path:path,review_sha256:value.result.review_sha256});reviewInfo.textContent=JSON.stringify(value.result,null,2);warning.hidden=false;status.textContent='Review the publisher, release, notices and exact files. Create draft keeps this skill in Skills for a separate publish decision.';}));
  create.addEventListener('click',()=>guarded(async ticket=>{if(!review||pending)return;await ensureSource(ticket);const body={...structuredClone(review),request_key:crypto.randomUUID()};const record={owner:scope.owner,request_key:body.request_key,body};try{sessionStorage.setItem(pendingKey(scope.owner),JSON.stringify(record));if(sessionStorage.getItem(pendingKey(scope.owner))!==JSON.stringify(record))throw new Error();}catch{throw new Error('Cannot retain the original request identity; draft was not issued.');}pending=record;review=null;controls();status.textContent='Creating the reviewed draft…';try{const value=await api('import',body);if(!validTicket(ticket))return;if(value?.ok!==true||!value.skill)throw new Error();sessionStorage.removeItem(pendingKey(scope.owner));pending=null;status.textContent='Draft created. Review it in Skills before publishing.';try{await onComplete(value.skill,()=>validTicket(ticket));}catch{}if(!validTicket(ticket))return;}catch{if(validTicket(ticket))status.textContent='Outcome unknown. Check original status; do not issue another import.';}}));
  check.addEventListener('click',()=>guarded(async ticket=>{const original=pending;if(!original)return;const response=await fetch(`/api/skills/marketplace/status?request_key=${encodeURIComponent(original.request_key)}`,{credentials:'same-origin',cache:'no-store',signal:reads.signal});if(!response.ok)throw new Error('Original status is unavailable.');const value=await response.json();if(!validTicket(ticket)||pending!==original)return;status.textContent=`Original status: ${String(value?.state || 'unknown')}`;if(value?.state==='complete'){sessionStorage.removeItem(pendingKey(scope.owner));pending=null;status.textContent='Original draft completed. Review it in Skills before publishing.';try{await onComplete(value,()=>validTicket(ticket));}catch{}}}));
  const changed=()=>{if(dialog.open)dialog.close();};
  for(const event of ['openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed','openclank-account-changed','openclank:auth-context-changed','workspace-change']) {window.addEventListener(event,changed,{signal:reads.signal});document.addEventListener(event,changed,{signal:reads.signal});}
  close.addEventListener('click',()=>dialog.close());dialog.addEventListener('close',()=>{disposed=true;epoch++;picker?.destroy();reads.abort();},{once:true});
  wireDialog(dialog);document.body.append(dialog);dialog.showModal();if(pending)status.textContent='An earlier request has an unknown outcome. Check original status before creating another draft.';controls();return true;
}
