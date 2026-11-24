import {wireDialog} from './copal/overlays.js';

/** Explicit selected-file review of one account preference; never a backup restore. */
export async function runBackupPreferenceTransfer({client,sourceRef,sourceResourceID,current,scopeRoot,status,
  getAccount=async({signal}={})=>{const r=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store',signal});if(!r.ok)throw new Error('Account unavailable');return String((await r.json())?.account_id||'');}}) {
  if(!scopeRoot?.isConnected || !current()) throw new Error('Files context unavailable.');
  const abort=new AbortController(),targets=[document,globalThis.window].filter(Boolean);
  const authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  const scopeEvents=['openclank-window-closed','odysseus:session-selected'];
  let closed=false,busy=false,issued=false,dialog,observer,resolveDone,account,prepared=null;
  const teardown=()=>{if(closed)return;closed=true;abort.abort();observer?.disconnect();for(const t of targets){for(const e of authEvents)t.removeEventListener(e,teardown);for(const e of scopeEvents)t.removeEventListener(e,checkScope);}prepared=null;if(dialog?.open)dialog.close();else dialog?.remove();resolveDone?.();};
  const checkScope=()=>{if(!closed&&(!scopeRoot.isConnected||!current()))teardown();};
  for(const t of targets){for(const e of authEvents)t.addEventListener(e,teardown);for(const e of scopeEvents)t.addEventListener(e,checkScope);}
  if(typeof MutationObserver==='function'){observer=new MutationObserver(checkScope);observer.observe(scopeRoot,{childList:true,subtree:true});if(scopeRoot.parentNode)observer.observe(scopeRoot.parentNode,{childList:true});}
  const guard=async()=>{if(closed||!scopeRoot.isConnected||!current())throw new Error('Files context changed.');const now=await getAccount({signal:abort.signal});if(!now||now!==account||closed||!current())throw new Error('Account changed.');};
  let page;try{account=await getAccount({signal:abort.signal});if(!account||!current())throw new Error('Account unavailable.');page=await client.workspaces({signal:abort.signal});await guard();}catch(error){teardown();throw error;}
  const workspaces=(page?.entries||[]).filter(row=>row?.availability==='available'&&typeof row?.workspace?.id==='string'&&row.workspace.id&&!row.workspace.archived);
  if(!workspaces.length){teardown();throw new Error('No owned Files workspace is available.');}
  dialog=document.createElement('dialog');dialog.className='copal-dialog';const heading=document.createElement('h2');heading.textContent='Review backup preference';
  const body=document.createElement('div');body.className='modal-body';const label=document.createElement('label');label.textContent='Owned workspace';
  const workspace=document.createElement('select');const empty=document.createElement('option');empty.value='';empty.textContent='Choose a workspace';workspace.append(empty);
  for(const row of workspaces){const option=document.createElement('option');option.value=row.workspace.id;option.textContent=row.workspace.name||row.workspace.id;workspace.append(option);}label.append(workspace);
  const message=document.createElement('p');message.setAttribute('aria-live','polite');body.append(label,message);
  const footer=document.createElement('div');footer.className='modal-footer';const button=text=>{const b=document.createElement('button');b.type='button';b.className='confirm-btn';b.textContent=text;footer.append(b);return b;};
  const close=button('Close'),preview=button('Preview'),apply=button('Apply to this account');dialog.append(heading,body,footer);document.body.append(dialog);
  const show=(text,error=false)=>{message.textContent=text;status?.(text,error);};
  const controls=()=>{workspace.disabled=busy||issued;preview.disabled=busy||issued||!workspace.value;apply.disabled=busy||issued||!prepared;apply.hidden=!prepared;};
  const request=async(body)=>{let response;try{response=await fetch('/api/session-transfer/backup-preference',{method:'POST',credentials:'same-origin',cache:'no-store',signal:abort.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(error){if(error?.name==='AbortError')throw error;return {state:'unknown'};}try{return await response.json();}catch{return {state:'unknown'};}};
  workspace.addEventListener('change',()=>{prepared=null;show('Choose Preview to inspect the selected file.');controls();});
  preview.addEventListener('click',async()=>{if(closed||busy||issued||!workspace.value)return;busy=true;prepared=null;controls();try{
    await guard();const found=await client.stat(sourceRef,{purpose:'snapshot',signal:abort.signal});await guard();
    if(found?.id!==sourceResourceID||found.provider!=='host'||found.kind!=='file'||!Array.isArray(found.capabilities)||!found.capabilities.includes('download')||typeof found.ref!=='string'||found.revision?.kind!=='hostObjectIdentity'||!/^[a-f0-9]{64}$/.test(found.revision.value||''))throw new Error('Selected file changed.');
    const base={resourceRef:found.ref,expectedRevision:found.revision,workspaceID:workspace.value,expectedAccountID:account};
    const result=await request({phase:'preview',...base});await guard();const source=result?.source,m=result?.mapping,old=result?.destinationBefore;
    if(result?.version!==1||result.state!=='preview'||result.accountID!==account||source?.resourceID!==sourceResourceID||source.workspaceID!==workspace.value||source.resourceRevision?.kind!==found.revision.kind||source.resourceRevision?.value!==found.revision.value||!/^[a-f0-9]{64}$/.test(source.sourceSHA256||'')||!Number.isSafeInteger(source.sourceBytes)||source.sourceBytes<1||source.sourceBytes>4*1024*1024||m?.sourceFormat!=='openclank-backup'||m.sourceVersion!==2||m.field!=='preferences.deliverable_reduced_motion'||m.destination!=='account.deliverable_reduced_motion'||typeof m.value!=='boolean'||!Number.isSafeInteger(m.unsupportedTopLevelCount)||m.unsupportedTopLevelCount<0||!Number.isSafeInteger(m.unsupportedPreferenceCount)||m.unsupportedPreferenceCount<0||!old||typeof old.present!=='boolean'||(old.present?typeof old.value!=='boolean':old.value!==null))throw new Error('This backup preference is unavailable or unsupported.');
    prepared={...base,sourceSHA256:source.sourceSHA256,value:m.value,destinationBefore:old};
    show(`Open Clank backup v2 · SHA-256 ${source.sourceSHA256}\nRecognized: reduced motion for deliverables = ${m.value?'On':'Off'}\nDestination: this account (currently ${old.present?(old.value?'On':'Off'):'not set'}).\nOther backup sections: ${m.unsupportedTopLevelCount}; other preferences: ${m.unsupportedPreferenceCount}. None will be applied.`);
  }catch(error){if(!closed)show(String(error?.message||'Preview unavailable.'),true);}finally{busy=false;if(!closed)controls();}});
  apply.addEventListener('click',async()=>{if(closed||busy||issued||!prepared)return;busy=true;controls();try{
    await guard();const exact=prepared;issued=true;controls();const result=await request({phase:'apply',...exact});await guard();
    if(result?.version===1&&result.state==='complete'&&result.accountID===account&&result.value===exact.value&&result.sourceSHA256===exact.sourceSHA256){document.dispatchEvent(new CustomEvent('openclank:deliverable-motion-refresh',{detail:{accountID:account}}));show('Preference saved for this account. Other backup data was not applied.');}
    else if(result?.state==='conflict')show('The account preference changed. Close and preview again.',true);
    else show('The outcome is unknown or unavailable. Check the current preference before deciding whether to start a new action.',true);
  }catch{if(!closed)show(issued?'The outcome is unknown. Check the current preference before deciding whether to start a new action.':'Account or source unavailable.',true);}finally{busy=false;if(!closed)controls();}});
  return await new Promise(resolve=>{resolveDone=resolve;close.addEventListener('click',teardown);dialog.addEventListener('close',teardown,{once:true});wireDialog(dialog);controls();if(closed){resolve();return;}dialog.showModal();});
}
