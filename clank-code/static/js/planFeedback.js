import { validateFeedbackScope } from './editor/feedbackScope.js';
export async function feedbackSelection(text,selection) {
  if(typeof text!=='string'||/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))throw new Error('Editor text is not valid Unicode.');
  const a=selection?.anchor,b=selection?.head;
  if(!Number.isSafeInteger(a)||!Number.isSafeInteger(b)||a<0||b<0||a>text.length||b>text.length)throw new Error('Editor selection is unavailable.');
  const from=Math.min(a,b),to=Math.max(a,b);if(from===to)return null;
  if((from>0 && /[\uD800-\uDBFF]/.test(text[from-1]) && /[\uDC00-\uDFFF]/.test(text[from]))||(to>0 && /[\uD800-\uDBFF]/.test(text[to-1]) && /[\uDC00-\uDFFF]/.test(text[to])))throw new Error('Selection splits a Unicode character.');
  const encoder=new TextEncoder(),start=encoder.encode(text.slice(0,from)).length,bytes=encoder.encode(text.slice(from,to));
  if(!bytes.length||bytes.length>8192)throw new Error('Selection exceeds 8192 bytes.');
  const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
  return {start,end:start+bytes.length,sha256:hash};
}

// One disposable view of host-owned plan comments.
export function mountPlanFeedback(dock, {apiBase, chat, plan, current, mode, captureTurnPolicy}) {
  if (!chat) return () => {};
  const hasPlan = Boolean(plan?.revision && plan?.digest);
  const box = document.createElement('section');
  box.dataset.planFeedback = '';
  const open = document.createElement('button'); open.type = 'button'; open.textContent = 'Feedback';
  const body = document.createElement('div'); body.hidden = true;
  const list = document.createElement('div');
  const batchReview = document.createElement('div'); batchReview.hidden = true; batchReview.setAttribute('aria-label','Reviewed feedback batch');
  const batchReviewButton = document.createElement('button'); batchReviewButton.type='button'; batchReviewButton.textContent='Review selected feedback';
  const batchSave = document.createElement('button'); batchSave.type='button'; batchSave.textContent='Save reviewed batch'; batchSave.hidden=true;
  let batchSelection=new Map(), batchPending=null;
  const input = document.createElement('textarea'); input.maxLength = 4096; input.setAttribute('aria-label', 'Plan feedback');
  const save = document.createElement('button'); save.type = 'button'; save.textContent = 'Save';
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  let selected = null, pendingSave = null, active = true, fileTarget=null, resourceTarget=null, targetCurrent=null, retargeting=false;
  const reads = new Set();
  const valid = () => active && current();
  function dispose() {
    if (!active) return;
    active = false;
    for (const read of reads) read.abort();
    reads.clear();
    input.value = ''; list.replaceChildren(); status.textContent = '';
    selected = null; pendingSave = null; batchSelection.clear(); batchPending=null; batchReview.replaceChildren(); fileTarget=null;resourceTarget=null;targetCurrent=null;plan = null; current = null; mode = null;
    document.removeEventListener('openclank:plan-feedback-target',setFileTarget);
    document.removeEventListener('openclank:resource-feedback-target',setResourceTarget);
    document.removeEventListener('openclank:widget-feedback-draft',setWidgetDraft);
    document.removeEventListener('openclank:resource-feedback-draft',setWidgetDraft);
    document.removeEventListener('openclank:design-feedback-target',setDesignTarget);
    document.removeEventListener('openclank:editor-feedback-scope-changed',checkTarget);
    document.removeEventListener('openclank:auth-context-changed', dispose);
    window.removeEventListener('openclank:auth-context-changed', dispose);
    document.removeEventListener('odysseus:session-selected', dispose);
    window.removeEventListener('workspace-change', dispose);
    window.removeEventListener('pagehide', dispose);
    box.remove();
  }
  document.addEventListener('openclank:auth-context-changed', dispose);
  const targetLabel=document.createElement('p');
  const designReview=document.createElement('div');designReview.hidden=true;designReview.setAttribute('aria-label','Reviewed design context');
  function showDesignSummary(container,design,{sourceName='',assetNames=[]}={}) {
    container.replaceChildren();
    const add=text=>{const line=document.createElement('p');line.textContent=text;container.append(line);};
    const appearance=design?.appearance || {}, standard=design?.standard || {}, typography=appearance.typography || {};
    add(`Open Clank design ${standard.version || 'unavailable'} · Theme: ${appearance.identity?.name || 'unavailable'} · Font: ${typography.font || 'unavailable'} · Density: ${typography.density || 'unavailable'} · Motion: ${appearance.motion?.reduced ? 'reduced' : 'standard'}`);
    if(design?.standardSource) add(`Selected standard: ${design.standardSource.version || 'pending validation'} · declared assets are not automatically verified.`);
    const colors=appearance.colors && typeof appearance.colors==='object' ? Object.entries(appearance.colors).map(([name,value])=>`${name} ${value}`).join(' · ') : '';
    if(colors)add(`Palette: ${colors}`);
    const output=design?.output || {};
    add(output.destination==='new-file' ? `Output: ${String(output.format || 'unavailable').toUpperCase()} · New file: ${output.name || 'unavailable'} · Folder: ${output.folder?.relativePath?.split('/').pop() || 'selected folder'} · Requested collision: fail. Creation is not yet verified.` : `Output: ${String(output.format || 'unavailable').toUpperCase()} · Destination: ${sourceName || 'selected source'} (in place)`);
    const assets=Array.isArray(design?.assets) ? design.assets : [];
    if(Number.isInteger(design?.appearanceAssetIndex))add(`Appearance: saved historical capture from selected asset ${design.appearanceAssetIndex+1}; verified on save and send.`);
    add(assets.length ? `Selected assets: ${assets.map((asset,index)=>assetNames[index] || (typeof asset.relativePath==='string' ? asset.relativePath.split('/').pop() : '') || `Asset ${index+1} (name unavailable)`).join(', ')}. Rechecked on save and send.` : 'Selected assets: none.');
  }
  async function resolveDesignNames(target,container) {
    const expected=target, assets=target.designContext?.assets || [];
    try {
      const {filesFacadeClient}=await import('./filesFacadeClient.js');
      if(!valid() || resourceTarget!==expected || !targetCurrent?.())return;
      const read=async(ref,revision)=>{const result=await filesFacadeClient.stat(ref);const row=result?.resource || result;if(!row || row.revision?.kind!==revision?.kind || row.revision?.value!==revision?.value)return '';return String(row.name || row.display_name || '').slice(0,256);};
      const names=await Promise.all([read(target.resourceRef,target.expectedRevision),...assets.map(asset=>read(asset.resourceRef,asset.expectedRevision))]);
      if(valid() && resourceTarget===expected && targetCurrent?.())showDesignSummary(container,target.designContext,{sourceName:names[0],assetNames:names.slice(1)});
    } catch (_) { /* Keep the honest unavailable labels until save reauthorizes Files. */ }
  }
  function setFileTarget(event){const detail=event.detail;if(!valid()||detail?.chat!==chat||typeof detail?.isCurrent!=='function'||!detail.isCurrent()||!hasPlan)return;fileTarget=detail.target;resourceTarget=null;targetCurrent=detail.isCurrent;selected=null;retargeting=false;pendingSave=null;body.hidden=false;targetLabel.textContent=fileTarget.selection?'Selected text · verify on save':'File · verify on save';detail.accepted=true;input.focus();}
  function setResourceTarget(event){const detail=event.detail;designReview.hidden=true;designReview.textContent='';if(!valid()||detail?.chat!==chat||typeof detail?.isCurrent!=='function'||!detail.isCurrent())return;let scope;try{scope=validateFeedbackScope(detail.target?.scope);}catch{status.textContent='Original task scope unavailable; choose the target again.';return;}resourceTarget={...detail.target,scope};fileTarget=null;targetCurrent=detail.isCurrent;if(!retargeting)selected=null;pendingSave=null;body.hidden=false;targetLabel.textContent=detail.label || 'File · verify on save';detail.accepted=true;input.focus();}
  function checkTarget(){if((fileTarget||resourceTarget)&&targetCurrent&&!targetCurrent()){pendingSave=null;status.textContent='Editor target changed; choose it again. Your comment is retained.';}}
  document.addEventListener('openclank:plan-feedback-target',setFileTarget);
  document.addEventListener('openclank:resource-feedback-target',setResourceTarget);
  function setWidgetDraft(event) { const d=event.detail; if (!valid() || d?.chat!==chat || typeof d?.isCurrent!=='function' || !d.isCurrent() || typeof d.text!=='string' || d.text.length>3500 || input.value.trim()) return; setResourceTarget(event); if (!d.accepted || !d.isCurrent()) { d.accepted=false; return; } input.value=d.text; input.dispatchEvent(new Event('input',{bubbles:true})); }
  document.addEventListener('openclank:widget-feedback-draft',setWidgetDraft);
  document.addEventListener('openclank:resource-feedback-draft',setWidgetDraft);
  function setDesignTarget(event){const d=event.detail;if(input.value.trim() || resourceTarget || fileTarget || selected || !d?.target?.designContext)return;setResourceTarget(event);if(d.accepted){targetLabel.textContent=d.label;showDesignSummary(designReview,resourceTarget.designContext);designReview.hidden=false;void resolveDesignNames(resourceTarget,designReview);}}
  document.addEventListener('openclank:design-feedback-target',setDesignTarget);


  document.addEventListener('openclank:editor-feedback-scope-changed',checkTarget);
  window.addEventListener('openclank:auth-context-changed', dispose);
  document.addEventListener('odysseus:session-selected', dispose);
  window.addEventListener('workspace-change', dispose);
  window.addEventListener('pagehide', dispose);
  const url = `${apiBase}/api/session/${encodeURIComponent(chat)}/plan/feedback`;
  async function refresh() {
    const controller = new AbortController(); reads.add(controller);
    let response, data;
    try {
      response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      data = await response.json();
    } finally { reads.delete(controller); }
    if (!valid()) return;
    if (!response.ok || !Array.isArray(data.entries)) throw new Error('Feedback is unavailable.');
    batchSelection.clear(); batchPending=null; batchReview.replaceChildren(); batchReview.hidden=true; batchSave.hidden=true;
    list.replaceChildren(...data.entries.map(entry => {
      const row = document.createElement('div');
      const text = document.createElement('p'); text.textContent = entry.text;
      const isResource=Boolean(entry.resourceTarget), isBatch=Boolean(entry.resourceBatchTarget), design=entry.resourceTarget?.designContext;
      const label = document.createElement('span'); label.textContent = isBatch ? `Saved batch · ${entry.delivery} · Files rechecked on send` : isResource ? `Saved · ${entry.delivery} · Files unchecked · anchor ${entry.anchorState}` : entry.current ? `Saved · ${entry.delivery}${entry.file?' · Files unchecked':''}` : `Saved · plan changed · ${entry.delivery}`;
      row.append(text, label);
      const describe=(item)=>`${item.resource?.relativePath || item.resource?.resourceID || 'Resource'} · ${item.resource?.revision?.value || 'revision unavailable'} · ${JSON.stringify(item.anchor)} · ${item.text}`;
      if (isResource && !design && entry.delivery==='saved' && entry.resourceTarget?.scope) {
        const choice=document.createElement('input');choice.type='checkbox';choice.setAttribute('aria-label','Include this saved feedback in a batch');
        choice.addEventListener('change',()=>{if(!valid())return;if(choice.checked && batchSelection.size>=8){choice.checked=false;status.textContent='Choose at most eight feedback items.';return;}
          if(choice.checked)batchSelection.set(entry.id,entry);else batchSelection.delete(entry.id);
          batchReview.replaceChildren();batchReview.hidden=true;batchSave.hidden=true;batchPending=null;});row.append(choice);
      }
      if(isBatch){const review=document.createElement('div');review.setAttribute('aria-label','Saved batch members');
        for(const item of entry.resourceBatchTarget.items || []){const line=document.createElement('p');line.textContent=describe(item);review.append(line);}row.append(review);}
      if(design){const review=document.createElement('div');review.setAttribute('aria-label','Saved design context; assets rechecked on send');showDesignSummary(review,design,{sourceName:entry.resourceTarget.resource?.relativePath?.split('/').pop() || ''});row.append(review);}
      if ((isResource || entry.file && entry.current) && entry.delivery==='saved') {
        const verify=document.createElement('button');verify.type='button';verify.textContent='Verify file';verify.addEventListener('click',async()=>{try{const response=await fetch(`${url}/${encodeURIComponent(entry.id)}/verify-file`,{method:'POST',credentials:'same-origin',cache:'no-store'});const result=await response.json();if(!valid())return;if(!response.ok)throw new Error();if(Array.isArray(result.designInputs)){const lines=result.designInputs.map((item,index)=>{const name=index===0?'Source':index===result.standardInputIndex?'Standard':`Asset ${index}`;return item.state==='unavailable'?`${name}: unavailable`:`${name}: ${item.state} · expected ${item.expectedRevision?.value || ''} · observed ${item.observedRevision?.value || ''}`;});if(result.designDestination)lines.push(`Output folder: ${result.designDestination.state}`);label.textContent=`Saved · ${entry.delivery} · ${lines.join(' | ')}. Reselect changed inputs in a new design handoff.`;}else{if(result.state!=='current')throw new Error();label.textContent=`Saved · ${entry.delivery} · Files current${isResource ? ` · anchor ${result.anchorState}` : ''}`;}if(result.resourceRef){const openFile=document.createElement('button');openFile.type='button';openFile.textContent='Open in Editor';openFile.addEventListener('click',async()=>{const stillCurrent=()=>valid() && row.isConnected;if(!stillCurrent())return;try{const editor=await import('./copal.js');const revision=isResource ? entry.resourceTarget.resource.revision : entry.file.revision;if(stillCurrent())await editor.openResource(result.resourceRef,{expectedRevision:revision,isCurrent:stillCurrent});}catch{if(stillCurrent())label.textContent='Saved · Files stale or unavailable';}});row.append(openFile);}}catch{if(valid())label.textContent='Saved · Files stale or unavailable';}});row.append(verify);
      }
      if (((isResource || isBatch || entry.current) && entry.delivery==='saved') || ['unknown','persisted','pending-boundary'].includes(entry.delivery)) {
        const send = document.createElement('button'); send.type = 'button';
        send.textContent = entry.delivery === 'saved' ? (isBatch ? 'Verify and send batch' : entry.file || isResource ? 'Verify and send' : 'Send') : 'Reconcile';
        send.addEventListener('click', async () => {
          if (!valid()) return;
          send.disabled = true;
          try {
            const controller = new AbortController(); reads.add(controller);
            let overviewResponse, overview;
            try {
              overviewResponse = await fetch(`${apiBase}/api/orchestration/${encodeURIComponent(chat)}`, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
              overview = await overviewResponse.json();
            } finally { reads.delete(controller); }
            if (!valid()) return;
            if (!overviewResponse.ok || !Number.isSafeInteger(overview.mapRevision)) throw new Error('Mapping unavailable');
            const scope=entry.resourceTarget?.scope || entry.resourceBatchTarget?.scope, actor=scope?.version===2 ? scope.actorID : 'main';
            if(scope?.version===2 && (scope.mapRevision!==overview.mapRevision || scope.rootSessionID!==overview.engineSessionId))throw new Error('Original actor mapping changed');
            const response = await fetch(`${apiBase}/api/orchestration/${encodeURIComponent(chat)}/actors/${encodeURIComponent(actor)}/input?map_revision=${overview.mapRevision}`, {
              method:'POST', credentials:'same-origin', cache:'no-store', headers:{'Content-Type':'application/json'},
              body:JSON.stringify({planFeedbackID:entry.id, mode:entry.mode || mode(),
                ...(entry.delivery === 'saved' ? {turnPolicy: (() => {
                  if (!valid() || typeof captureTurnPolicy !== 'function') throw new Error('Turn policy unavailable');
                  const policy = captureTurnPolicy();
                  if (!valid()) throw new Error('Turn policy unavailable');
                  return policy;
                })()} : {})})
            });
            const result = await response.json();
            if (!valid()) return;
            status.textContent = response.ok ? 'Checking the saved receipt…' : 'Delivery outcome is unknown; reconcile to check.';
            await refresh();
          } catch (_) { if (valid()) { list.replaceChildren(); status.textContent = 'Delivery outcome is unknown; reopen feedback to reconcile.'; } }
          finally { send.disabled = false; }
        });
        row.append(send);
      }
      if ((entry.current || isResource) && entry.delivery === 'saved' && !design) {
        const edit = document.createElement('button'); edit.type = 'button'; edit.textContent = 'Edit';
        edit.addEventListener('click', async () => { if (!valid()) return;try{if(entry.file||isResource){const response=await fetch(`${url}/${encodeURIComponent(entry.id)}/verify-file`,{method:'POST',credentials:'same-origin',cache:'no-store'});const proof=await response.json();if(!valid())return;if(!response.ok||proof.state!=='current')throw new Error();if(isResource){resourceTarget={resourceRef:proof.resourceRef,expectedRevision:entry.resourceTarget.resource.revision,anchor:entry.resourceTarget.anchor,scope:validateFeedbackScope(entry.resourceTarget.scope)};fileTarget=null;}else{fileTarget={resourceRef:proof.resourceRef,expectedRevision:entry.file.revision,...(entry.file.selection?{selection:entry.file.selection}:{})};resourceTarget=null;}targetCurrent=()=>valid();targetLabel.textContent='File verified · rechecked on save';}else{fileTarget=null;resourceTarget=null;targetCurrent=null;targetLabel.textContent='';}selected=entry;retargeting=false;input.value=entry.text;}catch{if(valid())status.textContent='File is stale or unavailable; comment retained. Choose Retarget to select a new target.';} }); row.append(edit);
        if(isResource && !design){const retarget=document.createElement('button');retarget.type='button';retarget.textContent='Retarget';retarget.addEventListener('click',()=>{if(!valid())return;selected=entry;retargeting=true;resourceTarget=null;fileTarget=null;targetCurrent=null;input.value=entry.text;status.textContent='Choose a new Editor target, then save.';});row.append(retarget);}
      }
      return row;
    }));
  }
  batchReviewButton.addEventListener('click',()=>{
    if(!valid())return;
    if(batchSelection.size<2 || batchSelection.size>8){status.textContent='Choose two to eight saved resource feedback items.';return;}
    batchReview.replaceChildren();
    for(const entry of batchSelection.values()){
      const target=entry.resourceTarget, line=document.createElement('p');
      line.textContent=`${target.resource?.relativePath || target.resource?.resourceID || 'Resource'} · ${target.resource?.revision?.value || 'revision unavailable'} · ${JSON.stringify(target.anchor)} · ${entry.text}`;
      batchReview.append(line);
    }
    batchReview.hidden=false;batchSave.hidden=false;status.textContent='Review every file, revision, anchor and comment before saving.';
  });
  batchSave.addEventListener('click',async()=>{
    if(!valid() || batchReview.hidden || batchSelection.size<2 || batchSelection.size>8)return;
    const members=[...batchSelection.values()].map(entry=>({feedbackID:entry.id,commentRevision:entry.comment_revision}));
    const signature=JSON.stringify(members);if(!batchPending || batchPending.signature!==signature)batchPending={signature,key:crypto.randomUUID()};
    batchSave.disabled=true;
    try{
      const response=await fetch(url,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_key:batchPending.key,batchMembers:members})});
      const data=await response.json();if(!valid())return;if(!response.ok || !data.entry)throw new Error('batch unavailable');
      batchPending=null;status.textContent='Batch saved. Review its members before sending.';await refresh();
    }catch(_){if(valid())status.textContent='Batch unavailable or a member changed; the original feedback remains saved.';}
    finally{batchSave.disabled=false;}
  });
  open.addEventListener('click', async () => {
    if (!valid()) return;
    body.hidden = !body.hidden;
    if (!body.hidden) try { await refresh(); } catch (_) { if (valid()) { list.replaceChildren(); status.textContent = 'Feedback is unavailable.'; } }
  });
  save.addEventListener('click', async () => {
    if (!valid()) return;
    if((fileTarget||resourceTarget) && (!targetCurrent || !targetCurrent())){status.textContent='Editor target changed; choose it again. Your comment is retained.';return;}
    if(retargeting && !resourceTarget){status.textContent='Choose a new Editor target before saving.';return;}
    if(!resourceTarget && !hasPlan){status.textContent='Choose a file target first.';return;}
    const draft = {text: input.value, ...(resourceTarget ? {resourceTarget} : {revision:plan.revision,digest:plan.digest,...(fileTarget?{fileTarget}:{})}),
      ...(selected ? {feedback_id:selected.id,comment_revision:selected.comment_revision} : {}), ...(retargeting ? {retarget:true} : {})};
    const signature = JSON.stringify(draft);
    if (!pendingSave || pendingSave.signature !== signature) pendingSave = {signature, key: crypto.randomUUID()};
    const value = {request_key: pendingSave.key, ...draft};
    save.disabled = true;
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type':'application/json'}, body: JSON.stringify(value)});
      const data = await response.json();
      if (!valid()) return;
      if (!response.ok || !data.entry) throw new Error('Feedback could not be saved.');
      selected = null; pendingSave = null; fileTarget=null;resourceTarget=null;targetCurrent=null;retargeting=false;targetLabel.textContent='';designReview.textContent='';designReview.hidden=true;input.value = ''; status.textContent = 'Saved.';
      try { await refresh(); } catch (_) { if (valid()) { list.replaceChildren(); status.textContent = 'Saved; feedback list is unavailable.'; } }
    } catch (_) { if (valid()) { list.replaceChildren(); status.textContent = resourceTarget ? 'Feedback could not be saved. If the task or file changed, choose the target again; your comment is retained.' : 'Feedback could not be saved; retry this comment to check.'; } }
    finally { save.disabled = false; }
  });
  body.append(list, batchReviewButton, batchReview, batchSave, targetLabel, designReview, input, save, status); box.append(open, body); dock.append(box);
  return dispose;
}
