import {wireDialog} from './copal/overlays.js';

// Explicit human observations use the existing Files workspace and receipts.
export async function runAnalysisNoteFlow({client, sourceRef, sourceResourceID, current, scopeRoot, status, openResult,
  getAccount = async ({signal} = {}) => {
    const response = await fetch('/api/auth/status', {credentials:'same-origin',cache:'no-store',signal});
    if ([401,403].includes(response.status)) return null;
    if (!response.ok) throw new Error('Account status unavailable');
    const account=String((await response.json())?.account_id || '');
    if(!account)throw new Error('Account status unavailable');
    return account;
  }}) {
  if(!scopeRoot?.isConnected) throw new Error('The originating Files surface is unavailable.');
  const reads=new AbortController();
  let closed=false,dialog=null,resolveDone=null,scopeObserver=null,fieldsToPurge=[],heldSave=null,heldInit=null,authorityUnavailable=false;
  const targets=[document,globalThis.window].filter(Boolean);
  const authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  const scopeEvents=['openclank-window-closed'];
  const surfaceEvents=['click','keydown','openclank:window-placement'];
  const teardown=()=>{
    if(closed)return;closed=true;reads.abort();scopeObserver?.disconnect();
    for(const target of targets){for(const event of authEvents)target.removeEventListener(event,teardown);for(const event of scopeEvents)target.removeEventListener(event,checkScope);}
    for(const event of surfaceEvents)scopeRoot.removeEventListener(event,checkScope);
    heldSave=null;heldInit=null;for(const field of fieldsToPurge)field.value='';
    if(dialog?.open)dialog.close(); else dialog?.remove();
    resolveDone?.();
  };
  let checkScheduled=false;
  const checkScope=()=>{if(checkScheduled||closed)return;checkScheduled=true;queueMicrotask(()=>{checkScheduled=false;if(!closed && !current())teardown();});};
  for(const target of targets){for(const event of authEvents)target.addEventListener(event,teardown);for(const event of scopeEvents)target.addEventListener(event,checkScope);}
  for(const event of surfaceEvents)scopeRoot.addEventListener(event,checkScope);
  if(typeof MutationObserver==='function'){
    scopeObserver=new MutationObserver(checkScope);
    scopeObserver.observe(scopeRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['class','aria-selected','hidden','style']});
    if(scopeRoot.parentNode)scopeObserver.observe(scopeRoot.parentNode,{childList:true});
  }
  let initialAccount;
  try { initialAccount=await getAccount({signal:reads.signal}); }
  catch(error){teardown();throw error;}
  if (closed || !initialAccount || !current()) { teardown(); throw new Error('The original account or selection is unavailable.'); }
  let scopeLost = false;
  const guard = async () => {
    if(closed || scopeLost || !current()){scopeLost=true;teardown();throw new Error('The original account or selection changed.');}
    let account;
    try { account=await getAccount({signal:reads.signal}); }
    catch (_) { if(closed)throw new Error('The original account or selection changed.');authorityUnavailable=true;throw new Error('Account status unavailable'); }
    if(closed || !current() || account===null || (account && account!==initialAccount)){
      scopeLost=true;teardown();throw new Error('The original account or selection changed.');
    }
    if(!account){authorityUnavailable=true;throw new Error('Account status unavailable');}
    authorityUnavailable=false;
  };
  let workspacePage;
  try { workspacePage=await client.workspaces({signal:reads.signal}); await guard(); }
  catch(error){teardown();throw error;}
  const workspaces = (workspacePage?.entries || []).filter(row => row?.availability === 'available'
    && typeof row?.workspace?.id === 'string' && row.workspace.id && !row.workspace.archived);
  if (!workspaces.length) { teardown(); throw new Error('No owned Files workspace is available.'); }
  dialog=document.createElement('dialog'); dialog.className='copal-dialog analysis-note-dialog';
  const heading=document.createElement('h2'); heading.textContent='Human analysis note'; dialog.append(heading);
  const fields=document.createElement('div'); fields.className='modal-body'; dialog.append(fields);
  const label=(name, element)=>{const row=document.createElement('label'); row.textContent=name; row.append(element); fields.append(row); return element;};
  const workspace=label('Owned workspace',document.createElement('select'));
  for(const row of workspaces){const option=document.createElement('option'); option.value=row.workspace.id; option.textContent=row.workspace.name || row.workspace.id; workspace.append(option);}
  const input=(name,value='',tag='input')=>{const el=document.createElement(tag); el.value=value; return label(name,el);};
  const id=input('Annotation ID','note'), title=input('Title'), text=input('Human observation','','textarea');
  const space=label('Address space (optional)',document.createElement('select'));
  for(const [value,name] of [['','None'],['file_offset','File offset'],['virtual','Virtual (human-unverified)'],['rva','RVA (human-unverified)']]){const option=document.createElement('option');option.value=value;option.textContent=name;space.append(option);}
  const start=input('Start (canonical lowercase 0x...)'), end=input('End exclusive (optional)');
  fieldsToPurge=[title,text,space,start,end,id];
  const detail=document.createElement('pre'); detail.setAttribute('aria-live','polite'); dialog.append(detail);
  const reloadConfirmation=document.createElement('div');reloadConfirmation.hidden=true;
  const reloadWarning=document.createElement('p');reloadWarning.textContent='Reloading replaces your current draft. Continue?';reloadConfirmation.append(reloadWarning);
  const replaceDraft=document.createElement('button'),keepDraft=document.createElement('button');
  replaceDraft.type=keepDraft.type='button';replaceDraft.textContent='Replace draft';keepDraft.textContent='Keep draft';
  reloadConfirmation.append(replaceDraft,keepDraft);dialog.append(reloadConfirmation);
  const footer=document.createElement('div'); footer.className='modal-footer'; dialog.append(footer);
  const button=(name)=>{const b=document.createElement('button');b.type='button';b.textContent=name;b.className='confirm-btn';footer.append(b);return b;};
  const close=button('Close'), retryAccount=button('Retry account check'), previewButton=button('Preview'), reload=button('Reload note'), initialize=button('Initialize project'), checkStatus=button('Check pending status'), save=button('Save note'), open=button('Open note');
  document.body.append(dialog);
  let busy=false,locked=false,preview=null,epoch=0,draftVersion=0,nonce=null,pendingStep=null,pendingCandidates=[],noteRef=null,reviewRequired=false,baselineTarget=null,pendingReload=null;
  const values=()=>({title:title.value,text:text.value,space:space.value,start:start.value,end:end.value});
  let baseline=values();
  const dirty=()=>JSON.stringify(values())!==JSON.stringify(baseline);
  const revisionKey=value=>value ? `${value.kind}:${value.value}` : '';
  const targetOf=value=>({workspace:workspace.value,id:id.value.trim(),state:value.note.state,revision:revisionKey(value.note.revision),
    sourceID:value.source.resourceID,sourceHash:value.source.sha256,sourceRevision:revisionKey(value.source.revision)});
  const sameTarget=(a,b)=>a && b && a.workspace===b.workspace && a.id===b.id && a.state===b.state && a.revision===b.revision
    && a.sourceID===b.sourceID && a.sourceHash===b.sourceHash && a.sourceRevision===b.sourceRevision;
  const adopt=value=>{
    const note=value.note;
    title.value=note.state==='missing'?'':note.title;text.value=note.state==='missing'?'':note.text;
    space.value=note.address?.space||'';start.value=note.address?.start||'';end.value=note.address?.endExclusive||'';
    baseline=values();baselineTarget=targetOf(value);reviewRequired=false;
  };
  const show=(message,error=false)=>{detail.textContent=message; status?.(message,error);};
  const validText=(value,limit,required=false)=>{
    if(typeof value!=='string' || (required && !value.length) || new TextEncoder().encode(value).length>limit
      || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)) throw new Error('Text is invalid or exceeds its limit.');
  };
  const controls=()=>{reloadConfirmation.hidden=!pendingReload;replaceDraft.disabled=busy||locked||authorityUnavailable;keepDraft.disabled=busy;
    retryAccount.disabled=busy||!authorityUnavailable;workspace.disabled=busy||locked||authorityUnavailable;id.disabled=busy||locked||authorityUnavailable;previewButton.disabled=busy||locked||authorityUnavailable;reload.disabled=busy||locked||authorityUnavailable||!preview;
    initialize.disabled=busy||locked||authorityUnavailable||!preview||preview.project.state!=='uninitialized'||!!pendingStep;
    checkStatus.disabled=busy||authorityUnavailable||(!pendingStep&&!pendingCandidates.length); save.disabled=busy||locked||authorityUnavailable||reviewRequired||!preview||preview.project.state!=='ready'||preview.note?.state==='historical'; open.disabled=busy||authorityUnavailable||!noteRef;};
  const invalidate=()=>{epoch++;preview=null;noteRef=null;pendingReload=null;controls();};
  workspace.addEventListener('change',()=>{if(busy)return;if(nonce || pendingStep){locked=true;show('A pending initialization belongs to the original workspace. Close and recover it before another attempt.',true);}if(dirty())reviewRequired=true;else baselineTarget=null;invalidate();});
  id.addEventListener('input',()=>{if(busy)return;if(dirty())reviewRequired=true;else baselineTarget=null;invalidate();});
  for(const field of [title,text,space,start,end]) field.addEventListener('input',()=>{draftVersion++;});
  const wrap=(fn)=>async()=>{if(busy||closed)return;busy=true;controls();try{await guard();await fn();}catch(error){if(closed)return;if(authorityUnavailable)show('Account status is unavailable. Your draft and any returned receipt are retained; retry the account check or copy the draft.',true);else show(String(error?.message||'Analysis note is unavailable.'),true);}finally{busy=false;if(!closed)controls();}};
  previewButton.addEventListener('click',wrap(async()=>{
    pendingReload=null;
    const token=++epoch, annotationID=id.value.trim();
    if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(annotationID))throw new Error('Use a lowercase annotation ID with letters, numbers, underscores or hyphens.');
    const value=await client.previewAnalysisProject({workspaceID:workspace.value,sourceRef,annotationID},{signal:reads.signal}); await guard();
    if(closed||token!==epoch)return;
    if(value?.workspaceID!==workspace.value || value?.source?.resourceID!==sourceResourceID || typeof value?.source?.ref!=='string'
       || !Number.isSafeInteger(value?.source?.size) || value.source.size<1 || value.source.size>16*1024*1024
       || !/^sha256:[0-9a-f]{64}:[0-9]+$/.test(value?.source?.revision?.value||'') || value.source.revision.kind!=='hostFingerprint'
       || !/^[0-9a-f]{64}$/.test(value?.source?.sha256||'') || !['ready','uninitialized'].includes(value?.project?.state)
       || !['missing','current','historical'].includes(value?.note?.state)
       || (value.note.state!=='missing' && (value.note.annotationID!==annotationID || typeof value.note.ref!=='string'
           || value.note.revision?.kind!=='hostFingerprint' || !/^sha256:[0-9a-f]{64}:[0-9]+$/.test(value.note.revision?.value||'')))) throw new Error('The exact source preview is unavailable.');
    preview=value; noteRef=value.note.ref || null;
    const nextTarget=targetOf(value);
    if(!dirty() && !reviewRequired) adopt(value);
    else if(!baselineTarget && value.note.state==='missing' && !reviewRequired) baselineTarget=nextTarget;
    else if(!sameTarget(baselineTarget,nextTarget)) reviewRequired=true;
    show(`Source: ${value.source.resourceID}\nSHA-256: ${value.source.sha256}\nSize: ${value.source.size} bytes\nProject: ${value.project.state}\nNote: ${value.note.state}${value.note.state==='historical'?' — historical source; editing is unavailable.':''}${reviewRequired?'\nThe note or target changed. Review it and choose Reload note before saving.':''}`);
  }));
  reload.addEventListener('click',wrap(async()=>{if(!preview)return;
    if(dirty()){pendingReload=preview;show('Reloading replaces your current draft. Choose Replace draft or Keep draft.');return;}
    adopt(preview);show('The displayed note was reloaded.');}));
  keepDraft.addEventListener('click',()=>{pendingReload=null;controls();});
  replaceDraft.addEventListener('click',wrap(async()=>{const target=pendingReload;pendingReload=null;
    if(!target || preview!==target)return;adopt(target);show('The displayed note was reloaded.');}));
  const presentHeldInit=async()=>{
    if(!heldInit)return;
    const {result,error,targetWorkspace}=heldInit;
    if(error){locked=true;
      try {
        const hash=async value=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(value))))].map(v=>v.toString(16).padStart(2,'0')).join('');
        const key='analysis-'+await hash(['openclank.analysis-project.v1',initialAccount,targetWorkspace]);
        const components=['.clanker','robonotes',key,'analysis'];
        pendingCandidates=await Promise.all(components.map(async(component,index)=>'ma2-init-'+await hash(['openclank.analysis-init.v1',initialAccount,targetWorkspace,nonce,index,component])));
      } catch (_) { pendingCandidates=[]; }
      await guard();if(closed)return;heldInit=null;show(pendingCandidates.length
        ? 'Initialization outcome is unknown. Check existing step status; do not start another attempt.'
        : 'Initialization outcome is unknown and step status is unavailable. Do not start another attempt.',true);return;}
    heldInit=null;
    if(workspace.value!==targetWorkspace){locked=true;show('The workspace changed during initialization. Keep the original request for recovery.',true);return;}
    if(result?.state==='pending'){pendingStep=result.operationID;show(`Initialization is pending at ${pendingStep}. Check its existing status before proceeding.`,true);return;}
    if(result?.state!=='ready'){locked=true;show('Initialization is partial or unknown. Keep this draft and do not retry blindly.',true);return;}
    nonce=null;invalidate();show('Project is ready. Preview the source again before saving.');
  };
  initialize.addEventListener('click',wrap(async()=>{
    if(!preview || preview.project.state!=='uninitialized')return;
    if(!nonce) nonce=crypto.randomUUID();
    const targetWorkspace=workspace.value;
    let result,error;
    try{result=await client.initializeAnalysisProject({workspaceID:targetWorkspace,operationID:nonce});}catch(caught){error=caught;}
    if(closed)return;heldInit={result,error,targetWorkspace};
    await guard();if(closed)return;await presentHeldInit();
  }));
  checkStatus.addEventListener('click',wrap(async()=>{
    if(pendingStep){const result=await client.operationReceipt(pendingStep,{generation:workspacePage.generation,signal:reads.signal});await guard();if(closed)return;
      if(result?.state==='complete'){pendingStep=null;show('The step completed. Select Initialize project to continue the same operation.');}
      else show(`Initialization remains ${result?.state||'pending'}; no new attempt was started.`,true);return;}
    let found=null;
    for(const candidate of pendingCandidates){if(closed)return;try{const result=await client.operationReceipt(candidate,{generation:workspacePage.generation,signal:reads.signal});await guard();if(closed)return;found={candidate,state:result?.state||'pending'};}catch(error){if(closed)return;await guard();}}
    if(closed)return;
    show(found?`Existing step ${found.candidate}: ${found.state}. The overall outcome remains unknown; no new attempt was started.`:'No exact step receipt is available. The outcome remains unknown; no new attempt was started.',true);
  }));
  const presentHeldSave=()=>{
    const held=heldSave;heldSave=null;if(!held)return;
    const {result,error,token,draft,capturedID,submitted,body,address}=held;
    if(error){const code=Number(error?.status);
      const definite=code===422 || (code>=400 && code<500 && ![408,429].includes(code)
        && ['analysis_pre_dispatch_refused','resource_changed','resource_ref_stale','invalid_resource_request','invalid_resource_ref','resource_unavailable','unsupported_operation'].includes(error?.code));
      if(definite){reviewRequired=true;preview=null;epoch++;show('Save was refused before a confirmed write. Keep this draft and preview for review.',true);return;}
      locked=true;show('Save outcome may be unknown. Keep or copy this draft; do not replay it.',true);return;}
    const receipt=result?.receipt;
    if(receipt?.outcome==='conflict'){reviewRequired=true;show('The note changed; preview and reload it before saving.',true);invalidate();return;}
    if(!['created','applied'].includes(receipt?.outcome)){locked=true;show('Save receipt is unavailable. Keep or copy this draft; do not replay it.',true);return;}
    if(typeof result.sourceCurrent!=='boolean' || receipt.revision?.kind!=='hostFingerprint'
       || !/^sha256:[0-9a-f]{64}:[0-9]+$/.test(receipt.revision?.value||'')
       || !['complete','incomplete','unavailable','not_applicable'].includes(receipt.history?.state)){
      locked=true;show('Save receipt is incomplete. Keep or copy this draft; do not replay it.',true);return;
    }
    if(token!==epoch || workspace.value!==body.workspaceID || id.value.trim()!==capturedID){locked=true;show(`The note for ${body.workspaceID} / ${capturedID} was saved; reopen that original target to review the receipt.`,true);return;}
    noteRef=receipt?.resource?.ref || noteRef;
    baseline=submitted;
    baselineTarget={workspace:body.workspaceID,id:capturedID,state:'current',revision:revisionKey(receipt.revision),
      sourceID:preview.source.resourceID,sourceHash:preview.source.sha256,sourceRevision:revisionKey(preview.source.revision)};
    preview={...preview,note:{state:'current',annotationID:capturedID,revision:receipt.revision,ref:noteRef,
      title:submitted.title,text:submitted.text,address:address||null}};
    reviewRequired=!result.sourceCurrent || receipt.readback==='changed' || receipt.changedAfterSave===true || receipt.changedAfterCreate===true;
    if(reviewRequired){preview=null;epoch++;}
    const newer=draft!==draftVersion?' Newer typing remains in the form.':'';
    show(`Note saved${result.sourceCurrent?'':' for the earlier source revision; source is no longer current'}; History: ${receipt.history?.state||'unavailable'}.${reviewRequired?' Preview and reload before another save.':''}${newer}`);
  };
  retryAccount.addEventListener('click',wrap(async()=>{if(heldSave)presentHeldSave();else if(heldInit)await presentHeldInit();else show('Account verified. Review the current draft before continuing.');}));
  save.addEventListener('click',wrap(async()=>{
    if(!preview || preview.project.state!=='ready' || preview.note?.state==='historical')return;
    const token=epoch, draft=draftVersion, capturedID=id.value.trim(), submitted=values();
    validText(title.value,200,true);validText(text.value,16384);
    if(!space.value && (start.value || end.value))throw new Error('Select an address space or clear the address.');
    if(space.value){
      const canonical=value=>/^0x(?:0|[1-9a-f][0-9a-f]*)$/.test(value) && value.length<=18;
      if(!canonical(start.value) || (end.value && !canonical(end.value)))throw new Error('Use canonical lowercase hexadecimal addresses.');
      const first=BigInt(start.value), last=end.value?BigInt(end.value):null;
      if(first>0xffffffffffffffffn || (last!==null && (last>0xffffffffffffffffn || last<=first))
         || (space.value==='file_offset' && (first>=BigInt(preview.source.size) || (last!==null && last>BigInt(preview.source.size)))))throw new Error('Address is outside its allowed range.');
    }
    const address=space.value ? {space:space.value,start:start.value,...(end.value ? {endExclusive:end.value} : {})} : undefined;
    const body={workspaceID:workspace.value,sourceRef:preview.source.ref,sourceRevision:preview.source.revision,annotationID:capturedID,title:title.value,text:text.value,
      ...(address?{address}:{}),...(preview.note.state==='current'?{expectedRevision:preview.note.revision}:{})};
    let result,error;
    try{result=await client.saveAnalysisObservation(body);}catch(caught){error=caught;}
    if(closed)return;
    heldSave={result,error,token,draft,capturedID,submitted,body,address};
    await guard();if(closed)return;presentHeldSave();
  }));
  open.addEventListener('click',wrap(async()=>{if(noteRef)await openResult(noteRef);}));
  return await new Promise(resolve=>{resolveDone=resolve;
    close.addEventListener('click',teardown);dialog.addEventListener('close',teardown,{once:true});
    wireDialog(dialog);controls();if(closed){resolve();return;}dialog.showModal();});
}

// A candidate is advisory until C1 verifies its exact persisted completed part.
export async function runAnalysisResultFlow({client,sourceRef,sourceResourceID,chatID,current,scopeRoot,status,openResult,openGraph = async (target,options)=>(await import('./copal.js')).openStaticFunctionOverlay(target,options),getAccount = async ({signal}={})=>{
  const response=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store',signal});
  if ([401,403].includes(response.status)) return null;
  if (!response.ok) throw new Error('Account unavailable');
  return String((await response.json())?.account_id||'');
}}) {
  if(!scopeRoot?.isConnected || !chatID || !current())throw new Error('The original chat or source is unavailable.');
  const reads=new AbortController(),targets=[document,globalThis.window].filter(Boolean);
  const authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  let closed=false,dialog,resolveDone,observer,account,unavailable=false,busy=false,locked=false,cursor=null,selected=null,completed=null,expectedRevision=null,held=null,saved=null,privateFields=[],snapshotRevision=null,snapshotPreview=null,snapshotPicker=null,snapshotFiles=[];
  const scopeEvents=['openclank-window-closed','odysseus:session-selected'];
  const teardown=()=>{if(closed)return;closed=true;reads.abort();snapshotPicker?.destroy();snapshotPreview=snapshotRevision=null;snapshotFiles=[];observer?.disconnect();for(const t of targets){for(const e of authEvents)t.removeEventListener(e,teardown);for(const e of scopeEvents)t.removeEventListener(e,checkScope);}for(const e of ['click','keydown','openclank:window-placement'])scopeRoot.removeEventListener(e,checkScope);selected=completed=held=saved=null;for(const field of privateFields){field.value='';field.textContent='';field.replaceChildren?.();}if(dialog?.open)dialog.close();else dialog?.remove();resolveDone?.();};
  let scheduled=false;const checkScope=()=>{if(scheduled||closed)return;scheduled=true;queueMicrotask(()=>{scheduled=false;if(!closed&&!current())teardown();});};
  for(const t of targets){for(const e of authEvents)t.addEventListener(e,teardown);for(const e of scopeEvents)t.addEventListener(e,checkScope);}
  for(const e of ['click','keydown','openclank:window-placement'])scopeRoot.addEventListener(e,checkScope);
  if(typeof MutationObserver==='function'){observer=new MutationObserver(checkScope);observer.observe(scopeRoot,{subtree:true,childList:true,attributes:true,attributeFilter:['class','aria-selected','hidden','style']});if(scopeRoot.parentNode)observer.observe(scopeRoot.parentNode,{childList:true});}
  const guard=async()=>{if(closed||!current()){teardown();throw new Error('The original chat or source changed.');}let next;try{next=await getAccount({signal:reads.signal});}catch{unavailable=true;throw new Error('Account unavailable. Retry to continue.');}if(closed||!current()||next===null||(next && next!==account)){teardown();throw new Error('The original account or source changed.');}if(!next){unavailable=true;throw new Error('Account unavailable. Retry to continue.');}unavailable=false;};
  try {account=await getAccount({signal:reads.signal});if(!account||!current())throw new Error('Account unavailable.');}catch(error){teardown();throw error;}
  let page;try{page=await client.workspaces({signal:reads.signal});await guard();}catch(error){teardown();throw error;}
  const workspaces=(page?.entries||[]).filter(row=>row?.availability==='available' && row?.workspace?.id && !row.workspace.archived);
  if(!workspaces.length){teardown();throw new Error('No owned Files workspace is available.');}
  dialog=document.createElement('dialog');dialog.className='copal-dialog analysis-note-dialog';
  const heading=document.createElement('h2');heading.textContent='Save verified analysis result';dialog.append(heading);
  const fields=document.createElement('div');fields.className='modal-body';dialog.append(fields);
  const label=(name,el)=>{const row=document.createElement('label');row.textContent=name;row.append(el);fields.append(row);return el;};
  const workspace=label('Owned workspace',document.createElement('select'));for(const row of workspaces){const option=document.createElement('option');option.value=row.workspace.id;option.textContent=row.workspace.name||row.workspace.id;workspace.append(option);}
  const dossier=label('Dossier ID',document.createElement('input'));dossier.value='analysis';
  const choices=label('Completed results in this chat',document.createElement('select'));
  const functions=label('Function (provider virtual address)',document.createElement('select'));
  const exportID=label('Function export ID',document.createElement('input'));exportID.value='functions';
  const snapshotDescription=label('Snapshot description',document.createElement('input'));snapshotDescription.maxLength=512;
  const snapshotMemoryID=label('Saved snapshot memory ID',document.createElement('input'));snapshotMemoryID.maxLength=512;
  const functionDetail=document.createElement('pre');functionDetail.setAttribute('aria-live','polite');fields.append(functionDetail);
  const detail=document.createElement('pre');detail.setAttribute('aria-live','polite');dialog.append(detail);
  privateFields=[dossier,choices,functions,exportID,snapshotDescription,snapshotMemoryID,functionDetail,detail];
  const footer=document.createElement('div');footer.className='modal-footer';dialog.append(footer);
  const button=name=>{const b=document.createElement('button');b.type='button';b.className='confirm-btn';b.textContent=name;footer.append(b);return b;};
  const close=button('Close'),retry=button('Retry account check'),list=button('List results'),more=button('Next page'),verify=button('Verify and review'),load=button('Load existing revision'),save=button('Save dossier'),open=button('Open dossier'),loadExport=button('Load export revision'),exportFunctions=button('Export functions'),graph=button('View static functions in Graph');
  const retainElf=button('Save ELF metadata');
  const snapshot=button('Capture folder snapshot'),compare=button('Compare captured folder');
  const prepareSnapshot=button('Review memory save'),issueSnapshot=button('Save snapshot to memory'),statusSnapshot=button('Check snapshot save');
  const resultActions=document.createElement('div');choices.parentNode.after(resultActions);resultActions.append(more);
  const functionActions=document.createElement('div');functionDetail.after(functionActions);functionActions.append(loadExport,exportFunctions,graph);
  const snapshotSection=document.createElement('details'),snapshotSummary=document.createElement('summary');snapshotSummary.textContent='Snapshot and memory';snapshotSection.append(snapshotSummary,snapshotDescription.parentNode,snapshotMemoryID.parentNode);
  const scopeLabel=document.createElement('label'),scopeMode=document.createElement('select');scopeLabel.textContent='Capture scope';for(const [value,title] of [['folder','Entire folder'],['selected','Selected files']]){const option=document.createElement('option');option.value=value;option.textContent=title;scopeMode.append(option);}scopeLabel.append(scopeMode);snapshotSection.append(scopeLabel);
  const fileSelection=document.createElement('div'),fileCount=document.createElement('span');const addFile=document.createElement('button'),clearFiles=document.createElement('button');for(const button of [addFile,clearFiles])button.type='button';addFile.textContent='Add file';clearFiles.textContent='Clear files';fileSelection.append(fileCount,addFile,clearFiles);snapshotSection.append(fileSelection);
  const budgetSection=document.createElement('details'),budgetSummary=document.createElement('summary');budgetSummary.textContent='Capture limits';budgetSection.append(budgetSummary);
  const budgetField=(title,value)=>{const row=document.createElement('label'),input=document.createElement('input');row.textContent=title;input.type='number';input.min='1';input.max='1024';input.step='1';input.value=String(value);row.append(input);budgetSection.append(row);return input;};
  const fileBudget=budgetField('Maximum per file (MiB)',64),totalBudget=budgetField('Maximum total (MiB)',256);snapshotSection.append(budgetSection);
  const snapshotInfo=document.createElement('pre');snapshotInfo.setAttribute('aria-live','polite');snapshotSection.append(snapshotInfo,snapshot,compare,prepareSnapshot,issueSnapshot,statusSnapshot);fields.append(snapshotSection);privateFields.push(snapshotInfo);
  document.body.append(dialog);
  const show=(text,error=false)=>{detail.textContent=text;status?.(text,error);};
  let functionMode=null,functionRevision=null,functionResult=null,exportSelection=null,exportRevision=null,graphTarget=null,overlayHandedOff=false,preparedSnapshot=null;
  const selectorKey=`openclank.analysis-snapshot.pending.v1:${account}`;
  const validSelector=value=>value&&Object.keys(value).sort().join(',')==='request_key,request_sha256'&&/^ma4_[0-9a-f]{64}$/.test(value.request_key||'')&&/^[0-9a-f]{64}$/.test(value.request_sha256||'');
  const pendingSelector=()=>{try{const value=JSON.parse(localStorage.getItem(selectorKey)||'null');return validSelector(value)?value:null;}catch{return null;}};
  const pendingExists=()=>{try{return localStorage.getItem(selectorKey)!==null;}catch{return true;}};
  const controls=()=>{for(const el of [workspace,dossier,choices,functions,exportID,snapshotDescription,snapshotMemoryID,scopeMode,fileBudget,totalBudget,addFile,clearFiles,list,more,verify,load,save,retainElf,open,loadExport,exportFunctions,graph,snapshot,compare,prepareSnapshot,issueSnapshot,statusSnapshot])el.disabled=busy||locked||unavailable;retry.hidden=!unavailable;retry.disabled=busy||!unavailable;more.hidden=!cursor;more.disabled ||= !cursor;verify.hidden=!selected||Boolean(completed);verify.disabled ||= !selected;save.hidden=!completed;save.disabled ||= !completed;open.hidden=!held?.ref;open.disabled ||= !held?.ref;functions.parentNode.hidden=!functionMode;functions.disabled ||= !functionMode;functionDetail.hidden=!functionMode;exportID.parentNode.hidden=functionMode!=='saved';loadExport.hidden=functionMode!=='saved'||!exportSelection;exportFunctions.hidden=functionMode!=='saved'||!exportSelection;exportFunctions.disabled ||= !exportSelection;graph.hidden=!graphTarget;graph.disabled ||= !graphTarget;snapshotSection.hidden=!snapshotRevision&&!snapshotPreview&&!pendingExists()&&!snapshotMemoryID.value.trim();scopeLabel.hidden=budgetSection.hidden=!snapshotRevision;fileSelection.hidden=!snapshotRevision||scopeMode.value!=='selected';fileCount.textContent=`${snapshotFiles.length} of 100 files selected. `;addFile.disabled ||= snapshotFiles.length>=100;clearFiles.disabled ||= !snapshotFiles.length;snapshot.hidden=!snapshotRevision;compare.hidden=!snapshotPreview&&!snapshotMemoryID.value.trim();snapshotDescription.parentNode.hidden=!snapshotPreview;prepareSnapshot.hidden=!snapshotPreview||!!preparedSnapshot||pendingExists();issueSnapshot.hidden=!preparedSnapshot||pendingExists();statusSnapshot.hidden=!pendingExists();statusSnapshot.disabled ||= !pendingSelector();};
  const wrap=fn=>async()=>{if(closed||busy)return;busy=true;controls();try{await guard();await fn();}catch(error){if(!closed)show(unavailable?'Account unavailable. Your selection is retained; retry before continuing.':String(error?.message||'Analysis result unavailable.'),true);}finally{busy=false;if(!closed)controls();}};
  const clearFunctions=()=>{functions.replaceChildren();functionDetail.textContent='';functionMode=functionRevision=functionResult=exportSelection=graphTarget=null;};
  const showFunctions=(result,mode,revision=null)=>{clearFunctions();if(!Array.isArray(result?.functions)||result.functions.length>256)return;const keys=new Map();for(const row of result.functions){if(!row||Object.keys(row).sort().join(',')!=='address,addressSpace,name,size'||typeof row.name!=='string'||row.addressSpace!=='provider_virtual'||!Number.isSafeInteger(row.address)||row.address<0||!Number.isSafeInteger(row.size)||row.size<0||!Number.isSafeInteger(row.address+row.size))continue;const key=JSON.stringify(row);keys.set(key,(keys.get(key)||0)+1);}const placeholder=document.createElement('option');placeholder.textContent='Select a function';functions.append(placeholder);for(const row of result.functions){if(keys.get(JSON.stringify(row))!==1)continue;const option=document.createElement('option');option.textContent=`${row.name||'(unnamed)'} — 0x${row.address.toString(16)} (${row.size})`;option._function={...row};functions.append(option);}if(functions.options.length>1){functionMode=mode;functionRevision=revision;functionResult=result;functionDetail.textContent='Select a function. Addresses are provider virtual, not file offsets; architecture is unknown. Disassembly, cross-references and traces are unavailable.';}};
  const clear=()=>{selected=completed=expectedRevision=cursor=exportRevision=snapshotRevision=snapshotPreview=preparedSnapshot=null;snapshotFiles=[];choices.replaceChildren();held=null;clearFunctions();};
  workspace.addEventListener('change',()=>{if(!busy){clear();controls();}});dossier.addEventListener('input',()=>{if(!busy){expectedRevision=exportRevision=snapshotRevision=snapshotPreview=preparedSnapshot=null;snapshotFiles=[];held=null;clearFunctions();controls();}});
  exportID.addEventListener('input',()=>{if(!busy){exportRevision=null;controls();}});
  snapshotDescription.addEventListener('input',()=>{if(!busy){preparedSnapshot=null;controls();}});
  snapshotMemoryID.addEventListener('input',()=>{if(!busy)controls();});
  const invalidateSnapshot=()=>{snapshotPreview=preparedSnapshot=null;snapshotInfo.textContent='Capture scope changed; capture again before saving.';controls();};
  scopeMode.addEventListener('change',()=>{if(!busy)invalidateSnapshot();});for(const budget of [fileBudget,totalBudget])budget.addEventListener('input',()=>{if(!busy)invalidateSnapshot();});
  clearFiles.addEventListener('click',()=>{if(!busy){snapshotFiles=[];invalidateSnapshot();}});
  addFile.addEventListener('click',wrap(async()=>{const {createResourcePicker}=await import('./copal/resourcePicker.js');await guard();
    const file=await new Promise(resolve=>{let chosen=null;const picker=createResourcePicker({client,purpose:'snapshot-file',accountScope:account,workspaceScope:workspace.value,isOriginCurrent:()=>!closed&&current(),rootLabel:'Choose a file inside the snapshot folder',isCompatible:resource=>resource.provider==='host'?'':'Choose a Host file.',onSelect:resource=>{chosen=resource;return true;},onClose:cancelled=>{if(snapshotPicker===picker)snapshotPicker=null;resolve(cancelled?null:chosen);}});snapshotPicker=picker;void picker.open();});
    await guard();if(!file)return;if(typeof file.ref!=='string'||!file.ref||snapshotFiles.includes(file.ref))throw new Error('Choose a distinct available file.');if(snapshotFiles.length>=100)throw new Error('The dialog supports up to 100 selected files.');snapshotFiles.push(file.ref);invalidateSnapshot();
  }));
  choices.addEventListener('change',()=>{selected=choices.selectedIndex>=0?choices.options[choices.selectedIndex]?._candidate:null;completed=null;clearFunctions();controls();});
  const listPage=async next=>{const preview=await client.previewAnalysisProject({workspaceID:workspace.value,sourceRef},{signal:reads.signal});await guard();if(preview?.source?.resourceID!==sourceResourceID||preview?.project?.state!=='ready')throw new Error('Initialize the selected workspace analysis project and preview this source first.');const request={workspaceID:workspace.value,sourceRef:preview.source.ref,stableChatID:chatID,...(next&&cursor?{cursor}:{})};const result=await client.analysisCandidates(request,{signal:reads.signal});await guard();choices.replaceChildren();if(!Array.isArray(result?.items)||result.items.length>20||typeof result.partial!=='boolean')throw new Error('Candidate page unavailable.');for(const item of result.items){if(!item||!['functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'].includes(item.operation)||typeof item.provisional!=='string')throw new Error('Candidate page unavailable.');const option=document.createElement('option');option.textContent=`${item.operation} — ${item.messageID}`;option._candidate=item;choices.append(option);}cursor=result.cursor||null;selected=choices.selectedIndex>=0?choices.options[choices.selectedIndex]?._candidate:null;completed=null;clearFunctions();show(result.partial?'Candidate page is partial; use Next page for more.':'Candidate page loaded. Verify a selected result before saving.');};
  list.addEventListener('click',wrap(()=>listPage(false)));more.addEventListener('click',wrap(()=>listPage(true)));
  verify.addEventListener('click',wrap(async()=>{if(!selected)return;const value=await client.completeAnalysisResult({provisional:selected.provisional,sourceRef},{signal:reads.signal});await guard();if(value?.state!=='complete'||typeof value.receipt!=='string'||!value.result)throw new Error('Exact completed result is unavailable.');completed=value;showFunctions(value.result,'local');const rendered=JSON.stringify(value.result,null,2).replace(/[\u202a-\u202e\u2066-\u2069]/g,char=>`\\u${char.charCodeAt(0).toString(16).padStart(4,'0')}`);const scope=selected.operation==='xrefs_from_instruction'?'Outgoing references from one selected provider-virtual instruction only; not a whole-function graph, file offset or runtime trace.':selected.operation==='decompile'?'Partial C-like provider interpretation; completion and asset/dependency identity are unverified, not recovered original source or runtime evidence.':selected.operation==='disassembly'?'Prior-provider virtual entry sample; not a complete function, file offset or runtime trace.':selected.operation==='xrefs_to_entry'?'Incoming references to the entry only; partial static provider evidence, not a complete caller graph or runtime trace.':'Provider evidence is partial.';show(`Verified ${selected.operation}; ${scope} Review before saving.\n${rendered}`);}));
  load.addEventListener('click',wrap(async()=>{if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(dossier.value))throw new Error('Invalid dossier ID.');const value=await client.readAnalysisDossier({workspaceID:workspace.value,dossierID:dossier.value,sourceRef},{signal:reads.signal});await guard();if(value?.state!=='verified'||value.revision?.kind!=='hostFingerprint')throw new Error('Existing revision unavailable.');expectedRevision=value.revision;snapshotRevision=value.sourceState==='current'&&value.resultIdentity?.provider==='rizin'&&value.resultIdentity?.providerVersion==='0.8.1'&&value.resultIdentity?.executableEvidence==='native_producer_attested'&&value.resultIdentity?.versionEvidence==='operator_configured_unverified'||value.sourceState==='current'&&value.resultIdentity?.producer==='openclank.elf-metadata'&&value.resultIdentity?.producerVersion==='elf-v1-metadata-1'&&value.resultIdentity?.producerEvidence==='host_owned_capture'&&value.resultIdentity?.coverage==='partial_declared_metadata'&&value.resultIdentity?.payloadIntegrity==='not_checked'?value.revision:null;snapshotPreview=null;held=value;showFunctions(value.result,'saved',value.revision);if(Array.isArray(value.result?.functions)&&value.result.functions.length<=256)graphTarget=value.revision;show(`Existing dossier loaded (${value.sourceState}). Saving will use its exact revision.`);}));
  const chooseSnapshotRoot=async()=>{
    const {createResourcePicker}=await import('./copal/resourcePicker.js');await guard();
    const selectedRoot=await new Promise(resolve=>{let chosen=null;const picker=createResourcePicker({client,purpose:'folder',accountScope:account,
      workspaceScope:workspace.value,isOriginCurrent:()=>!closed&&current(),rootLabel:'Choose a folder in the owned workspace',
      isCompatible:resource=>resource.provider==='host'?'':'Choose a Host folder.',onSelect:resource=>{chosen=resource;return true;},
      onClose:cancelled=>{if(snapshotPicker===picker)snapshotPicker=null;resolve(cancelled?null:chosen);}});snapshotPicker=picker;void picker.open();});
    await guard();if(!selectedRoot)return null;
    const value=await client.stat(selectedRoot.ref,{signal:reads.signal});await guard();const root=value?.resource||value;
    if(root?.provider!=='host'||root?.kind!=='folder'||!root.ref||!root.revision||
       (selectedRoot.resourceId&&root.resource_id&&selectedRoot.resourceId!==root.resource_id))throw new Error('The selected folder changed.');
    return {rootRef:root.ref,rootRevision:root.revision};
  };
  snapshot.addEventListener('click',wrap(async()=>{if(!snapshotRevision)return;const capability=await client.analysisSnapshotCapabilities({signal:reads.signal});await guard();
    if(capability?.version!==1||!capability.defaultLimits)throw new Error('Snapshot capability unavailable.');
    const fileBytes=Number(fileBudget.value)*1048576,totalBytes=Number(totalBudget.value)*1048576;
    if(!Number.isInteger(Number(fileBudget.value))||!Number.isInteger(Number(totalBudget.value))||!Number.isSafeInteger(fileBytes)||!Number.isSafeInteger(totalBytes)||fileBytes<1048576||totalBytes<1048576||fileBytes>capability.maximumFileBytes||totalBytes>capability.maximumTotalBytes)throw new Error('Choose limits within the available range.');
    const limits={...capability.defaultLimits,file_bytes:fileBytes,total_bytes:totalBytes};
    snapshotInfo.textContent=`Choose a folder. Capture is bounded to ${limits.nodes} entries, ${fileBytes} bytes per file and ${totalBytes} bytes total over ${limits.seconds} seconds.`;
    if(scopeMode.value==='selected'&&!snapshotFiles.length)throw new Error('Select at least one file.');
    const root=await chooseSnapshotRoot();if(!root)return;const value=await client.previewAnalysisSnapshot({workspaceID:workspace.value,...root,mode:scopeMode.value,selectedRefs:scopeMode.value==='selected'?[...snapshotFiles]:[],dossierID:dossier.value,expectedDossierRevision:snapshotRevision,sourceRef,limits},{signal:reads.signal});await guard();
    if(value?.state!=='ready'||typeof value.previewToken!=='string'||!value.preview)throw new Error('Snapshot preview unavailable.');snapshotPreview={token:value.previewToken,root};
    preparedSnapshot=null;snapshotInfo.textContent=`Captured interval: ${value.preview.coverage}; ${value.preview.componentCount} components (${value.preview.shown} shown). Upload retention is temporary until saved.\n${JSON.stringify(value.preview.components,null,2)}`;
  }));
  compare.addEventListener('click',wrap(async()=>{const memoryID=snapshotMemoryID.value.trim();if(!snapshotPreview&&!memoryID)return;const root=await chooseSnapshotRoot();if(!root)return;
    const value=await client.compareAnalysisSnapshot({workspaceID:workspace.value,...(memoryID?{memoryID}:{previewToken:snapshotPreview.token}),...root},{signal:reads.signal});await guard();
    if(value?.state!=='compared'||!Array.isArray(value.comparison?.components))throw new Error('Comparison unavailable.');
    show(`Comparison is between observed intervals; unknown or unavailable is not absence.\n${JSON.stringify(value.comparison,null,2)}`);
  }));
  const snapshotPost=async body=>{const response=await fetch('/api/memory/analysis-snapshot/publication',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error('Snapshot save outcome unavailable. Check status.');return result;};
  prepareSnapshot.addEventListener('click',wrap(async()=>{if(!snapshotPreview||pendingExists())return;const description=snapshotDescription.value.trim();if(!description||new TextEncoder().encode(description).length>2048||/[\u0000-\u001f\u007f]/.test(description))throw new Error('Enter a short single-line snapshot description.');
    const result=await snapshotPost({phase:'prepare',preview_token:snapshotPreview.token,description});await guard();
    if(result?.state!=='prepared'||!validSelector(result.selector)||result.capture_origin!=='verified_capture'||result.description!==description)throw new Error('Snapshot review unavailable.');
    preparedSnapshot={selector:result.selector,token:snapshotPreview.token,description};show('Review the captured components and description. Save snapshot to memory only when ready.');
  }));
  const finishApproved = result => {
    const report = result.component_history;
    snapshotMemoryID.value = result.curated_id;
    if (!report || report.state === 'incomplete' || report.unavailable > 0) {
      show('Snapshot saved. Component history is incomplete. Use Check snapshot save to retry with the captured folder.', true);
      return;
    }
    try { localStorage.removeItem(selectorKey); } catch {}
    show(report.state === 'disabled' ? 'Snapshot saved. Automatic component history is disabled.' :
      `Snapshot saved. Component history includes ${report.owned} saved here and ${report.shared} in Files History.`);
  };
  issueSnapshot.addEventListener('click',wrap(async()=>{if(!preparedSnapshot||pendingExists())return;const original=preparedSnapshot;
    const raw=JSON.stringify(original.selector);try{localStorage.setItem(selectorKey,raw);if(localStorage.getItem(selectorKey)!==raw)throw new Error();}catch{throw new Error('Recovery storage unavailable; snapshot was not sent.');}
    preparedSnapshot=null;controls();let result;try{result=await snapshotPost({phase:'issue',preview_token:original.token,description:original.description,selector:original.selector,component_folder:{workspaceID:workspace.value,...snapshotPreview.root}});}catch{show('Save outcome unknown. Use Check snapshot save; do not issue again.',true);return;}
    if(result?.state!=='applied'||typeof result.curated_id!=='string'||!result.curated_id||result.curated_id.length>512){show('Save outcome unknown. Use Check snapshot save; do not issue again.',true);return;}
    try{await guard();}catch{return;}finishApproved(result);
  }));
  statusSnapshot.addEventListener('click',wrap(async()=>{const selector=pendingSelector();if(!selector)return;const folder=snapshotPreview?.root || await chooseSnapshotRoot();if(!folder)return;const result=await snapshotPost({phase:'status',selector,component_folder:{workspaceID:workspace.value,...folder}});await guard();
    if(result?.state!=='applied'||typeof result.curated_id!=='string'||!result.curated_id||result.curated_id.length>512){show('Save remains unknown. Check status later; it will not be issued again.');return;}
    finishApproved(result);
  }));
  graph.addEventListener('click',wrap(async()=>{if(!graphTarget)return;const capturedWorkspace=workspace.value,capturedDossier=dossier.value,capturedRevision=graphTarget;const scopeCurrent=()=>current()&&(closed?overlayHandedOff:workspace.value===capturedWorkspace&&dossier.value===capturedDossier&&graphTarget===capturedRevision);await guard();const opened=await openGraph({workspaceID:capturedWorkspace,dossierID:capturedDossier,expectedDossierRevision:capturedRevision,sourceRef,accountID:account,client,watchRoot:scopeRoot},{isCurrent:scopeCurrent});if(!opened)throw new Error('Graph view unavailable.');overlayHandedOff=true;teardown();}));
  functions.addEventListener('change',wrap(async()=>{exportSelection=null;const row=functions.options[functions.selectedIndex]?._function;if(!row||!functionMode)return;if(functionMode==='local'){functionDetail.textContent=`${row.name||'(unnamed)'} — provider virtual 0x${row.address.toString(16)}, size ${row.size}. Partial static evidence; ${functionResult.omitted} omitted; architecture unknown.\nSource SHA-256: ${functionResult.sourceSHA256}; revision: ${JSON.stringify(functionResult.sourceRevision)}\nProvider: ${functionResult.provider} ${functionResult.providerVersion}; version ${functionResult.providerVersionEvidence}. Result hash is available in a verified saved dossier.\nDisassembly, cross-references and traces unavailable.`;return;}const value=await client.inspectAnalysisFunction({workspaceID:workspace.value,dossierID:dossier.value,expectedDossierRevision:functionRevision,selector:row,sourceRef},{signal:reads.signal});await guard();if(value?.state!=='verified'||value.function?.addressSpace!=='provider_virtual'||JSON.stringify(value.function)!==JSON.stringify(row))throw new Error('Function projection unavailable.');exportSelection={...row};functionDetail.textContent=`${value.function.name||'(unnamed)'} — provider virtual 0x${value.function.address.toString(16)}, size ${value.function.size}. Source: ${value.sourceState}; partial static evidence, ${value.omitted} omitted; architecture unknown. One function selected for export.\n${JSON.stringify(value.resultIdentity,null,2)}\nDisassembly, cross-references and traces unavailable.`;}));
  loadExport.addEventListener('click',wrap(async()=>{if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(exportID.value))throw new Error('Invalid export ID.');const value=await client.readAnalysisFunctionExport({workspaceID:workspace.value,exportID:exportID.value},{signal:reads.signal});await guard();if(value?.state!=='available'||value.revision?.kind!=='hostFingerprint')throw new Error('Export revision unavailable.');exportRevision=value.revision;show('Existing function export loaded; its exact revision will be used.');}));
  exportFunctions.addEventListener('click',wrap(async()=>{if(!exportSelection||functionMode!=='saved'||!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(exportID.value))throw new Error('Select a verified saved function and enter an export ID.');const body={workspaceID:workspace.value,dossierID:dossier.value,expectedDossierRevision:functionRevision,selectors:[exportSelection],exportID:exportID.value,sourceRef,...(exportRevision?{expectedExportRevision:exportRevision}:{})};let value;try{value=await client.exportAnalysisFunctions(body);}catch(error){locked=true;show('Export outcome is unavailable or unknown. Do not replay; retain the original target for review.',true);return;}const receipt=value?.receipt;if(!['created','applied','unchanged'].includes(receipt?.outcome)||receipt.revision?.kind!=='hostFingerprint'||!/^sha256:[0-9a-f]{64}:[0-9]+$/.test(receipt.revision?.value||'')||!['complete','incomplete','unavailable','not_applicable'].includes(receipt.history?.state)||value.functionCount!==1){locked=true;show('Export receipt unavailable. Do not replay.',true);return;}saved={...value,_uiKind:'Function export'};exportSelection=null;held=receipt.resource||null;try{await guard();}catch{return;}show(`One function ${receipt.outcome==='unchanged'?'unchanged':'exported'}; History: ${receipt.history.state}; source at export: ${value.sourceStateAtExport}.`);}));
  retainElf.addEventListener('click',wrap(async()=>{if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(dossier.value))throw new Error('Enter a valid dossier ID.');const preview=await client.previewAnalysisProject({workspaceID:workspace.value,sourceRef},{signal:reads.signal});await guard();if(preview?.source?.resourceID!==sourceResourceID||preview?.project?.state!=='ready')throw new Error('Initialize the analysis project and select the same source.');const body={workspaceID:workspace.value,sourceRef:preview.source.ref,sourceRevision:preview.source.revision,dossierID:dossier.value,...(expectedRevision?{expectedRevision}:{})};let result;try{result=await client.saveAnalysisElfDossier(body);}catch(error){if(Number(error?.status)===409&&error?.code==='analysis_pre_dispatch_refused'){show('ELF inspection was refused before saving. Choose a supported source and continue.',true);return;}locked=true;show('Save outcome is unavailable or unknown. Do not replay; retain the original target for review.',true);return;}const receipt=result?.receipt;if(!['created','applied','unchanged'].includes(receipt?.outcome)||receipt.revision?.kind!=='hostFingerprint'||!/^sha256:[0-9a-f]{64}:[0-9]+$/.test(receipt.revision?.value||'')||!['complete','incomplete','unavailable','not_applicable'].includes(receipt.history?.state)||typeof result.sourceCurrent!=='boolean'){locked=true;show('Save receipt unavailable. Do not replay.',true);return;}held=receipt.resource||null;saved={...result,_uiKind:'ELF metadata'};await guard();show(`ELF metadata ${receipt.outcome==='unchanged'?'unchanged':'saved'}; History: ${receipt.history?.state||'unavailable'}; source ${result.sourceCurrent?'current':'historical'}.`);}));
  save.addEventListener('click',wrap(async()=>{if(!completed||!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(dossier.value))throw new Error('Verify the result and enter a valid dossier ID.');const target={workspaceID:workspace.value,sourceRef,completedReceipt:completed.receipt,dossierID:dossier.value,...(expectedRevision?{expectedRevision}:{})};let result;try{result=await client.saveAnalysisDossier(target);}catch(error){locked=true;show('Save outcome is unavailable or unknown. Do not replay; retain the original target for review.',true);return;}held=result?.receipt?.resource||null;const receipt=result?.receipt;if(!['created','applied','unchanged'].includes(receipt?.outcome)||receipt.revision?.kind!=='hostFingerprint'||!/^sha256:[0-9a-f]{64}:[0-9]+$/.test(receipt.revision?.value||'')||!['complete','incomplete','unavailable','not_applicable'].includes(receipt.history?.state)||typeof result.sourceCurrent!=='boolean'){locked=true;show('Save receipt unavailable. Do not replay.',true);return;}saved={...result,_uiKind:'Dossier'};completed=null;try{await guard();}catch{return;}show(`Dossier ${receipt.outcome==='unchanged'?'unchanged':'saved'}; History: ${receipt.history.state}; source ${result.sourceCurrent?'current':'historical'}${receipt.readback==='changed'||receipt.changedAfterSave||receipt.changedAfterCreate?'; destination changed after save':''}.`);}));
  retry.addEventListener('click',wrap(async()=>show(saved?`${saved._uiKind} ${saved.receipt.outcome==='unchanged'?'unchanged':'saved'}; History: ${saved.receipt.history?.state||'unavailable'}.`:'Account verified; review the selection before continuing.')));open.addEventListener('click',wrap(async()=>{if(held?.ref)await openResult(held.ref);}));
  return await new Promise(resolve=>{resolveDone=resolve;close.addEventListener('click',teardown);dialog.addEventListener('close',teardown,{once:true});wireDialog(dialog);controls();if(closed){resolve();return;}dialog.showModal();});
}

// The saved copy is selected by Memory ID; no original source or preview is required.
export async function runSavedSnapshotCompare({client,memoryID,scopeRoot,current,status}) {
  if(typeof memoryID!=='string'||!memoryID||memoryID.length>512||!scopeRoot?.isConnected||!current())throw new Error('Saved snapshot unavailable.');
  const abort=new AbortController();let closed=false,dialog,picker,resolveDone,account,observer;
  const authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  const targets=[document,globalThis.window].filter(Boolean);
  const close=()=>{if(closed)return;closed=true;abort.abort();picker?.destroy();observer?.disconnect();for(const target of targets){for(const event of authEvents)target.removeEventListener(event,close);for(const event of ['openclank-window-closed','odysseus:session-selected'])target.removeEventListener(event,checkScope);}if(dialog?.open)dialog.close();else dialog?.remove();resolveDone?.();};
  const checkScope=()=>{if(!scopeRoot.isConnected||!current())close();};
  for(const target of targets)for(const event of authEvents)target.addEventListener(event,close);
  for(const target of targets)for(const event of ['openclank-window-closed','odysseus:session-selected'])target.addEventListener(event,checkScope);
  if(typeof MutationObserver==='function'&&scopeRoot.parentNode){observer=new MutationObserver(checkScope);observer.observe(scopeRoot.parentNode,{childList:true});}
  const check=async()=>{if(closed||!scopeRoot.isConnected||!current()){close();throw new Error('The original account or selection changed.');}const response=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store',signal:abort.signal});if(!response.ok)throw new Error('Account unavailable. Retry when connected.');const next=String((await response.json())?.account_id||'');if(closed||!scopeRoot.isConnected||!current()||(account&&next!==account)){close();throw new Error('The original account or selection changed.');}if(!next)throw new Error('Account unavailable.');return next;};
  try{account=await check();const page=await client.workspaces({signal:abort.signal});await check();const workspaces=(page?.entries||[]).filter(row=>row?.availability==='available'&&row?.workspace?.id&&!row.workspace.archived);if(!workspaces.length)throw new Error('No owned Files workspace is available.');
    dialog=document.createElement('dialog');dialog.className='copal-dialog analysis-note-dialog';const title=document.createElement('h2');title.textContent='Compare saved snapshot';dialog.append(title);
    const body=document.createElement('div');body.className='modal-body';dialog.append(body);const label=document.createElement('label');label.textContent='Owned workspace';const workspace=document.createElement('select');label.append(workspace);body.append(label);
    for(const row of workspaces){const option=document.createElement('option');option.value=row.workspace.id;option.textContent=row.workspace.name||row.workspace.id;workspace.append(option);}
    const detail=document.createElement('pre');detail.setAttribute('aria-live','polite');detail.textContent='Choose the current folder captured by this snapshot. Comparison observes an interval; unknown is not absence.';body.append(detail);
    const footer=document.createElement('div');footer.className='modal-footer';dialog.append(footer);const cancel=document.createElement('button');cancel.type='button';cancel.textContent='Close';const run=document.createElement('button');run.type='button';run.className='confirm-btn';run.textContent='Choose folder and compare';footer.append(cancel,run);document.body.append(dialog);
    cancel.addEventListener('click',close);dialog.addEventListener('close',close,{once:true});run.addEventListener('click',async()=>{run.disabled=true;try{await check();const {createResourcePicker}=await import('./copal/resourcePicker.js');await check();const chosen=await new Promise(resolve=>{let selected=null;const p=createResourcePicker({client,purpose:'folder',accountScope:account,workspaceScope:workspace.value,isOriginCurrent:()=>!closed&&scopeRoot.isConnected&&current(),rootLabel:'Choose the captured folder',isCompatible:resource=>resource.provider==='host'?'':'Choose a Host folder.',onSelect:resource=>{selected=resource;return true;},onClose:cancelled=>{if(picker===p)picker=null;resolve(cancelled?null:selected);}});picker=p;void p.open();});await check();if(!chosen)return;const raw=await client.stat(chosen.ref,{signal:abort.signal});await check();const root=raw?.resource||raw;if(root?.provider!=='host'||root?.kind!=='folder'||!root.ref||!root.revision)throw new Error('Folder unavailable.');const result=await client.compareAnalysisSnapshot({workspaceID:workspace.value,memoryID,rootRef:root.ref,rootRevision:root.revision},{signal:abort.signal});await check();if(result?.state!=='compared'||!Array.isArray(result.comparison?.components))throw new Error('Comparison unavailable.');detail.textContent=`Observed intervals; unavailable or unknown is not absence.\n${JSON.stringify(result.comparison,null,2)}`;status?.('Saved snapshot compared.');}catch(error){if(!closed){detail.textContent=String(error?.message||'Comparison unavailable.');status?.(detail.textContent,true);}}finally{if(!closed)run.disabled=false;}});
    wireDialog(dialog);return await new Promise(resolve=>{resolveDone=resolve;dialog.showModal();});
  }catch(error){close();throw error;}
}

export async function runSnapshotPublicationStatus({scopeRoot,current,status}) {
  if(!scopeRoot?.isConnected||!current())throw new Error('Memory view unavailable.');
  const controller=new AbortController();let invalid=false;const targets=[document,globalThis.window].filter(Boolean);
  const events=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  const invalidate=()=>{invalid=true;controller.abort();};for(const target of targets)for(const event of events)target.addEventListener(event,invalidate);
  const account=async()=>{if(invalid||!scopeRoot.isConnected||!current())throw new Error('Memory view changed.');const response=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error('Account unavailable.');const value=String((await response.json())?.account_id||'');if(!value||invalid||!scopeRoot.isConnected||!current())throw new Error('Account unavailable.');return value;};
  try{const original=await account(),key=`openclank.analysis-snapshot.pending.v1:${original}`;let selector;try{selector=JSON.parse(localStorage.getItem(key)||'null');}catch{throw new Error('Snapshot recovery selector unavailable.');}
    if(!selector||Object.keys(selector).sort().join(',')!=='request_key,request_sha256'||!/^ma4_[0-9a-f]{64}$/.test(selector.request_key||'')||!/^[0-9a-f]{64}$/.test(selector.request_sha256||''))throw new Error('No valid pending snapshot save is available.');
    const response=await fetch('/api/memory/analysis-snapshot/publication',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({phase:'status',selector}),signal:controller.signal});const result=await response.json();if(!response.ok||await account()!==original)throw new Error('Snapshot save status unavailable.');
    if(result?.state==='applied'&&typeof result.curated_id==='string'&&result.curated_id&&result.curated_id.length<=512){if(result.component_history?.state==='incomplete'){status?.('Snapshot saved. Open its Files analysis and choose the captured folder to retry component history.');}else{try{localStorage.removeItem(key);}catch{}status?.('Snapshot save confirmed.');}return {state:'applied',memoryID:result.curated_id};}
    status?.('Snapshot save remains unknown; check status later.');return {state:'unknown'};
  }finally{for(const target of targets)for(const event of events)target.removeEventListener(event,invalidate);}
}
