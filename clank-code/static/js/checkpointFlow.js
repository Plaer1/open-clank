import {wireDialog} from './copal/overlays.js';

// The reviewed refs and operation IDs are memory-only selectors, never authority.
export async function runCheckpointFlow({client,entries,chooseRoot,current,scopeRoot,status,openHistory,openCompare,openRestore}) {
  if (!scopeRoot?.isConnected || !current() || !Array.isArray(entries) || entries.length<1 || entries.length>16) throw new Error('Choose one to sixteen Host files.');
  const selectedRoot=await chooseRoot();
  if (!selectedRoot?.ref || !scopeRoot?.isConnected || !current()) return;
  const reads=new AbortController(),targets=[document,window],authEvents=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank:file-policy-changed'];
  const scopeEvents=['openclank-window-closed','odysseus:session-selected'];
  let closed=false,busy=false,root=null,review=null,registrationID=null,captureID=null,registrationIssued=false,captureIssued=false,registered=false,registrationState=null,captureState=null,resolveDone,observer;
  const dialog=document.createElement('dialog');dialog.className='styled-dialog';dialog.setAttribute('aria-label','Review selected checkpoint');
  const title=document.createElement('h3');title.textContent='Review selected checkpoint';dialog.append(title);
  const note=document.createElement('p');note.textContent='Select an authorized root and preview the current files. Preview prepares the existing private Files credential. Capture observes an interval; it is not an atomic snapshot.';dialog.append(note);
  const list=document.createElement('div');dialog.append(list);const comparison=document.createElement('select');comparison.setAttribute('aria-label','File to compare');dialog.append(comparison);const message=document.createElement('p');message.setAttribute('role','status');dialog.append(message);
  const footer=document.createElement('div');footer.className='modal-footer';dialog.append(footer);
  const button=(text)=>{const b=document.createElement('button');b.type='button';b.className='confirm-btn';b.textContent=text;footer.append(b);return b;};
  const pick=button('Preview selected files'),register=button('Add files to History'),capture=button('Capture checkpoint'),check=button('Check status'),history=button('Open History'),compare=button('Compare file'),restore=button('Restore selected captured file'),close=button('Close');document.body.append(dialog);
  const clear=()=>{root=review=null;registrationID=captureID=null;registrationState=captureState=null;registered=false;list.replaceChildren();};
  const teardown=()=>{if(closed)return;closed=true;reads.abort();observer?.disconnect();for(const t of targets){for(const e of authEvents)t.removeEventListener(e,teardown);for(const e of scopeEvents)t.removeEventListener(e,checkScope);}for(const e of ['click','keydown','openclank:window-placement'])scopeRoot.removeEventListener(e,checkScope);clear();if(dialog.open)dialog.close();else dialog.remove();resolveDone?.();};
  const checkScope=()=>{if(!closed&&!current())teardown();};
  for(const t of targets){for(const e of authEvents)t.addEventListener(e,teardown);for(const e of scopeEvents)t.addEventListener(e,checkScope);}
  for(const e of ['click','keydown','openclank:window-placement'])scopeRoot.addEventListener(e,checkScope);
  observer=new MutationObserver(checkScope);observer.observe(scopeRoot,{attributes:true,attributeFilter:['hidden','class','style']});
  const controls=()=>{pick.hidden=!!review||registrationIssued||captureIssued;pick.disabled=busy;register.hidden=!review||registrationIssued;register.disabled=busy||!review?.supported;capture.hidden=!registered||captureIssued;capture.disabled=busy;check.hidden=!(registrationIssued||captureIssued);check.disabled=busy;history.hidden=captureState?.state!=='complete';history.disabled=busy;comparison.hidden=restore.hidden=captureState?.state!=='complete';compare.hidden=comparison.hidden||captureState?.current_mapping!=='current';comparison.disabled=compare.disabled=restore.disabled=busy;};
  const guard=()=>{if(closed||!scopeRoot.isConnected||!current()){teardown();throw new Error('The Files selection or account changed.');}};
  const requestBody=(id,only)=>({root_ref:root.ref,root_revision:root.revision,items:review.items.map(item=>({resource_ref:item.ref,expected_revision:item.revision})),request_id:id,status_only:only});
  const action=fn=>async()=>{if(busy||closed)return;busy=true;controls();try{guard();await fn();guard();}catch(error){if(!closed){message.textContent=String(error?.message||'Checkpoint unavailable.');status?.(message.textContent,true);}}finally{busy=false;if(!closed)controls();}};
  pick.addEventListener('click',action(async()=>{
    guard();
    const parent=await client.stat(selectedRoot.ref,{signal:reads.signal});guard();if(parent?.provider!=='host'||!['folder','directory'].includes(parent.kind)||!parent.ref||!parent.revision)throw new Error('Selected root is unavailable.');
    const verified=[];let total=0;
    for(const entry of entries){const row=await client.stat(entry.resource_ref,{purpose:'checkpoint',signal:reads.signal});guard();const fp=row?.revision?.value;if(row?.provider!=='host'||row.kind!=='file'||row.id!==entry.resource_id||row.revision?.kind!=='hostFingerprint'||!/^sha256:[a-f0-9]{64}:(0|[1-9][0-9]*)$/.test(fp||''))throw new Error('A selected file changed or is unavailable.');const size=Number(fp.split(':')[2]);if(!Number.isSafeInteger(size)||size>16*1024*1024||(total+=size)>64*1024*1024)throw new Error('Selection exceeds checkpoint limits.');verified.push({ref:row.ref,revision:row.revision,name:String(row.name||entry.name||'File'),size});}
    const cap=await client.checkpointCapabilities({root_ref:parent.ref,root_revision:parent.revision},{signal:reads.signal});guard();root={ref:parent.ref,revision:parent.revision};review={items:verified,supported:cap?.supported===true};list.replaceChildren();for(const row of verified){const p=document.createElement('p');p.textContent=`${row.name} · ${row.size} bytes · ${row.revision.value}`;list.append(p);}comparison.replaceChildren();verified.forEach((row,index)=>{const option=document.createElement('option');option.value=String(index);option.textContent=row.name;comparison.append(option);});message.textContent=review.supported?'Review the exact files, then explicitly add them to History.':'Checkpoint support is unavailable for this root or History profile.';
  }));
  register.addEventListener('click',action(async()=>{registrationID=crypto.randomUUID().replaceAll('-','');registrationIssued=true;registrationState=await client.checkpointRegistration(requestBody(registrationID,false));guard();registered=registrationState.state==='complete'&&registrationState.items.every(item=>['registered','observed'].includes(item.state));message.textContent=registered?'Files are registered. Review, then capture.':registrationState.state==='pending'?'Registration may still be running. Check the original status; it will not be reissued.':'Some files could not be registered. Check status.';}));
  capture.addEventListener('click',action(async()=>{captureID=crypto.randomUUID().replaceAll('-','');captureIssued=true;captureState=await client.checkpointCapture(requestBody(captureID,false));guard();message.textContent=captureState.state==='complete'?'Checkpoint captured for the reviewed interval.':captureState.state==='failed'?'Checkpoint failed before capture.':'Capture may still be running. Check the original status; it will not be reissued.';}));
  check.addEventListener('click',action(async()=>{if(captureIssued){captureState=await client.checkpointCapture(requestBody(captureID,true));message.textContent=captureState.state==='complete'?`Checkpoint complete; current mapping ${captureState.current_mapping}.`:captureState.state==='failed'?'Checkpoint failed.':'Original checkpoint remains pending or unavailable.';}else{registrationState=await client.checkpointRegistration(requestBody(registrationID,true));registered=registrationState.state==='complete'&&registrationState.items.every(item=>['registered','observed'].includes(item.state));message.textContent=registered?'Files are registered. Review, then capture.':'Original registration remains pending or incomplete.';}}));
  compare.addEventListener('click',()=>{
    if(closed||busy||captureState?.state!=='complete'||captureState?.current_mapping!=='current'||!current())return;
    const index=Number(comparison.value),entry=entries[index],publicID=captureState.items?.[index]?.resource_id;
    if(!entry||publicID!==entry.resource_id)return;
    const payload={root_ref:root.ref,original_root_revision:root.revision,items:review.items.map(item=>({resource_ref:item.ref,expected_revision:item.revision})),request_id:captureID,resource_id:publicID};
    teardown();void openCompare(entry,({signal}={})=>client.compareCheckpoint(payload,{signal}),payload.request_id);
  });
  restore.addEventListener('click',()=>{
    if(closed||busy||captureState?.state!=='complete'||!current())return;
    const index=Number(comparison.value),entry=entries[index],publicID=captureState.items?.[index]?.resource_id;
    if(!entry||publicID!==entry.resource_id)return;
    const checkpointID=captureID;teardown();void openRestore?.(entry,checkpointID,publicID);
  });
  history.addEventListener('click',()=>{if(closed||busy||captureState?.state!=='complete'||!current())return;const first=entries[0];teardown();void openHistory(first);});
  return await new Promise(resolve=>{resolveDone=resolve;close.addEventListener('click',teardown);dialog.addEventListener('close',teardown,{once:true});wireDialog(dialog);controls();dialog.showModal();});
}
