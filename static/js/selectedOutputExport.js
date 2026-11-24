import {wireDialog} from './copal/overlays.js';

/** Review a finite selection; the preview token is a selector, never Files authority. */
export async function reviewSelectedOutputs({entries,client,current,scopeRoot}) {
  if (!scopeRoot?.isConnected || !current() || !Array.isArray(entries) || entries.length<1 || entries.length>16) throw Error('Selection unavailable.');
  const dialog=document.createElement('dialog');dialog.className='styled-dialog';dialog.setAttribute('aria-label','Review selected outputs');
  const title=document.createElement('h3');title.textContent='Review selected outputs';dialog.append(title);
  const note=document.createElement('p');note.textContent='Only the selected files are included. This observes an interval, not an atomic snapshot. Content-secret scanning and license verification are unavailable; preparing a download does not publish.';dialog.append(note);
  const input=(label,placeholder)=>{const wrap=document.createElement('label');wrap.textContent=label;const field=document.createElement('textarea');field.placeholder=placeholder;field.maxLength=2048;wrap.append(field);dialog.append(wrap);return field;};
  const license=input('License facts','Declare facts or leave blank for unknown');
  const attribution=input('Attribution facts','Declare facts or leave blank for unknown');
  const omissions=input('Omitted outputs','One explicitly omitted output per line');
  const privacyLabel=document.createElement('label'),privacy=document.createElement('input');privacy.type='checkbox';privacyLabel.append(privacy,document.createTextNode(' I reviewed these outputs for private data; this is not a safety certification.'));dialog.append(privacyLabel);
  const list=document.createElement('pre');list.textContent=entries.map(e=>String(e.name||'Selected file')).join('\n');dialog.append(list);
  const status=document.createElement('p');status.setAttribute('role','status');dialog.append(status);
  const footer=document.createElement('div');footer.className='modal-footer';dialog.append(footer);
  const preview=document.createElement('button'),download=document.createElement('button'),close=document.createElement('button');
  preview.type=download.type=close.type='button';preview.textContent='Preview exact outputs';download.textContent='Download reviewed ZIP';download.hidden=true;close.textContent='Close';footer.append(preview,download,close);document.body.append(dialog);
  const abort=new AbortController();let closed=false,busy=false,plan=null,observer;
  const valid=()=>!closed && scopeRoot.isConnected && current();
  const retire=()=>{if(closed)return;closed=true;abort.abort();observer?.disconnect();document.removeEventListener('openclank:auth-context-changed',retire);document.removeEventListener('openclank:file-policy-changed',retire);if(dialog.open)dialog.close();else dialog.remove();};
  document.addEventListener('openclank:auth-context-changed',retire);document.addEventListener('openclank:file-policy-changed',retire);
  observer=new MutationObserver(()=>{if(!valid())retire();});observer.observe(scopeRoot,{attributes:true,attributeFilter:['hidden','class','style']});observer.observe(document.body,{childList:true,subtree:true});
  close.addEventListener('click',retire);dialog.addEventListener('close',retire,{once:true});
  preview.addEventListener('click',async()=>{if(busy||!valid())return;busy=true;preview.disabled=true;
    try {const items=entries.map(e=>({ref:e.resource_ref,id:e.resource_id,revision:e.revision}));
      const value=await client.previewSelectedOutputs({items,omissions:omissions.value.split('\n').map(x=>x.trim()).filter(Boolean),license:license.value,attribution:attribution.value,privacy:privacy.checked},{signal:abort.signal});
      if(!valid()||value?.version!==1||typeof value.previewToken!=='string'||!Array.isArray(value.members)||value.members.length!==entries.length)throw Error('Preview unavailable.');
      plan=value;list.textContent=value.members.map(m=>`${m.archivePath} · ${m.size} bytes · SHA256 ${m.sha256}${m.findings?.length?' · '+m.findings.join('; '):''}`).join('\n')+'\n\nOmissions: '+(value.review.omissions.join('; ')||'none declared')+'\nLicense: '+(value.review.license||'unknown')+'\nAttribution: '+(value.review.attribution||'unknown');
      for(const field of [license,attribution,omissions,privacy])field.disabled=true;preview.hidden=true;download.hidden=false;status.textContent='Review the exact list and findings before downloading.';
    }catch(e){if(valid())status.textContent=String(e?.message||'Preview unavailable.');}finally{busy=false;preview.disabled=false;}});
  download.addEventListener('click',async()=>{if(busy||!valid()||!plan)return;busy=true;download.disabled=true;
    try{const blob=await client.exportSelectedOutputs(plan.previewToken,entries[0].resource_ref,{signal:abort.signal});if(!valid())return;
      const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='selected-outputs.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);status.textContent='Reviewed ZIP download prepared. Nothing was published.';
    }catch(e){if(valid())status.textContent=String(e?.message||'Reviewed export unavailable or changed.');}finally{busy=false;if(valid())download.disabled=false;}});
  wireDialog(dialog);dialog.showModal();
}
