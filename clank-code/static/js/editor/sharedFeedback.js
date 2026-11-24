/** Explicit O15-shaped feedback for an already authorized shared resource. */
export async function openSharedFeedback({publicationID, expectedRevision, anchor={kind:'whole'}, isCurrent, canComment=false}={}) {
  if (!/^[a-f0-9]{32}$/.test(String(publicationID || '')) || typeof isCurrent !== 'function' || !isCurrent()) throw Error('Shared feedback is unavailable.');
  const dialog=document.createElement('dialog'), title=document.createElement('h2'), status=document.createElement('p'), list=document.createElement('ul');
  const input=document.createElement('textarea'), save=document.createElement('button'), close=document.createElement('button');
  title.textContent='Shared feedback';status.setAttribute('role','status');input.setAttribute('aria-label','Feedback');input.maxLength=4096;
  save.type=close.type='button';save.textContent='Save feedback';close.textContent='Close';save.disabled=true;
  dialog.append(title,status,list);
  let anchorChoice=null;
  if(anchor?.kind==='slide') {const selected=document.createElement('label');selected.textContent='Feedback target ';anchorChoice=document.createElement('select');anchorChoice.setAttribute('aria-label','Feedback target');const object=document.createElement('option');object.value='object';object.textContent='Current slide text object: '+String(anchor.slideID)+' / '+String(anchor.objectID);const whole=document.createElement('option');whole.value='whole';whole.textContent='Whole shared resource';anchorChoice.append(object,whole);selected.append(anchorChoice);dialog.append(selected);}
  if(canComment)dialog.append(input,save);dialog.append(close);document.body.append(dialog);
  let closed=false, issued=false, revision=null, editing=null;
  const current=()=>!closed && dialog.isConnected && isCurrent();
  function done(){closed=true;if(dialog.open)dialog.close();dialog.remove();}
  close.addEventListener('click',done);dialog.addEventListener('close',()=>{closed=true;dialog.remove();},{once:true});
  async function call(path,body){
    if(!current())throw Error('Shared resource changed.');
    const response=await fetch(`/api/files-v1/shared-feedback/${path}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!current())throw Error('Shared resource changed.');
    if(!response.ok)throw Error('Shared feedback is unavailable or changed.');
    const value=await response.json();if(!current())throw Error('Shared resource changed.');return value;
  }
  async function load(){
    const result=await call('list',{version:1,publication_id:publicationID});
    if(result.version!==1 || !Number.isSafeInteger(result.revision) || result.revision<0 || !Array.isArray(result.entries) || result.entries.length>64)throw Error('Shared feedback is unavailable.');
    revision=result.revision;list.replaceChildren();
    for(const entry of result.entries){
      if(!entry || typeof entry.text!=='string' || typeof entry.authorLabel!=='string' || typeof entry.authorIsCurrent!=='boolean' || !entry.source?.revision)throw Error('Shared feedback is unavailable.');
      const row=document.createElement('li'), label=document.createElement('strong'), body=document.createElement('p');
      label.textContent=entry.authorLabel;body.textContent=entry.text;
      row.append(label,body);
      if(entry.anchor?.kind==='image') {const where=document.createElement('small');where.textContent='Image region';row.append(where);}
      if(entry.anchor?.kind==='cell') {const where=document.createElement('small');where.textContent='Workbook cell/range · '+String(entry.anchor.sheetID)+' / '+String(entry.anchor.range?.startCellID || '')+' – '+String(entry.anchor.range?.endCellID || '');row.append(where);}
      if(entry.anchor?.kind==='slide') {const where=document.createElement('small');where.textContent='Slide text object · '+String(entry.anchor.slideID)+' / '+String(entry.anchor.objectID);row.append(where);}
      if(canComment && entry.authorIsCurrent && expectedRevision && JSON.stringify(entry.source.revision)===JSON.stringify(expectedRevision)){
        const edit=document.createElement('button');edit.type='button';edit.textContent='Edit';edit.addEventListener('click',()=>{if(!current() || issued)return;editing=entry;input.value=entry.text;status.textContent='Editing your saved feedback.';});row.append(edit);
      }
      if(expectedRevision && JSON.stringify(entry.source.revision)!==JSON.stringify(expectedRevision)){const note=document.createElement('small');note.textContent='Captured on an earlier revision.';row.append(note);}list.append(row);
    }
    status.textContent=result.entries.length?'Feedback is saved on this shared resource.':'No visible feedback on this shared resource.';
    save.disabled=!canComment || !expectedRevision;
  }
  save.addEventListener('click',async()=>{
    if(!current() || issued || save.disabled || !input.value.trim())return;
    const text=input.value, key=crypto.randomUUID(), selected=editing;issued=true;save.disabled=true;status.textContent='Saving…';
    try{
      const result=await call('save',{version:1,publication_id:publicationID,request_key:key,text,expected_revision:expectedRevision,annotation_revision:revision,anchor:selected?.anchor || (anchorChoice?.value==='whole'?{kind:'whole'}:anchor),...(selected?{feedback_id:selected.id,comment_revision:selected.comment_revision}:{})});
      if(result.version!==1 || !Number.isSafeInteger(result.revision) || !result.entry?.id)throw Error('Receipt unavailable.');
      input.value='';editing=null;issued=false;await load();
    }catch(_){if(current())status.textContent='The outcome may be unknown. Reopen feedback to inspect it; do not repeat automatically.';}
  });
  dialog.showModal();try{await load();}catch(error){if(current())status.textContent=error.message || 'Shared feedback unavailable.';}
  return {close:done};
}
