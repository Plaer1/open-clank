import { wireDialog } from './copal/overlays.js';

// Publication IDs and returned URLs are transient selectors, never authority.
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key,canonical(value[key])])) : value;
const same = (a,b) => a != null && b != null && JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
const el = (tag,text) => {const node=document.createElement(tag);if(text!=null)node.textContent=String(text);return node;};
function label(text,control) {const node=el('label',text);node.append(control);return node;}
function select(name, values) {const node=el('select');node.setAttribute('aria-label',name);for(const [value,title] of values){const option=el('option',title);option.value=value;node.append(option);}node.value=values[0][0];return node;}
function safeUrl(value) {
  try {const url=new URL(value,window.location.origin);if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||!/^\/api\/files\/download\/[A-Za-z0-9_-]+$/.test(url.pathname))return null;return url.href;}catch(_){return null;}
}
function options(audience, expiry) {
  const value=String(expiry.value||'').trim();
  if (!['owner','public'].includes(audience.value) || (value && (!/^\d+$/.test(value) || Number(value)<1 || Number(value)>(audience.value==='public'?720:8760)))) throw new Error('Choose a valid expiry in hours.');
  return {audience:audience.value,expires_in_hours:value ? Number(value) : null};
}
function dialogBase(title,notice,wire) {
  const dialog=el('dialog');dialog.className='styled-dialog';dialog.setAttribute('aria-label',title);
  const heading=el('h3',title), note=el('p',notice), body=el('div'), message=el('p');message.setAttribute('role','status');
  const footer=el('div');footer.className='modal-footer';dialog.append(heading,note,body,message,footer);document.body.append(dialog);wire(dialog);
  const button=text=>{const node=el('button',text);node.type='button';node.className='confirm-btn';footer.append(node);return node;};
  return {dialog,body,message,button};
}
function watchScope(dialog,current,onStale) {
  const targets=[document,window], events=['openclank:auth-context-changed','openclank-account-changed','openclank-files-policy-changed',
    'openclank-policy-changed','openclank:file-policy-changed','openclank-window-closed','odysseus:session-selected','chat-workspace-change'];
  const check=()=>{if(!current())onStale();};
  for(const target of targets) for(const event of events) target.addEventListener?.(event,check);
  dialog.addEventListener('close',()=>{for(const target of targets) for(const event of events) target.removeEventListener?.(event,check);},{once:true});
}
async function request(fetchImpl,path,{method='GET',body,signal}={}) {
  const response=await fetchImpl(path,{method,credentials:'same-origin',cache:'no-store',...(signal===undefined?{}:{signal}),...(body===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  const payload=await response.json().catch(()=>null);
  if (!response.ok) {const error=new Error(response.status>=500?'The outcome may be unknown. Open Manage shared downloads before trying again.':'The request was not accepted. Check access and the public URL setting.');error.unknown=response.status>=500;throw error;}
  if (!payload || typeof payload!=='object' || Array.isArray(payload)) {const error=new Error('The outcome may be unknown. Open Manage shared downloads before trying again.');error.unknown=true;throw error;}
  return payload;
}
function resultControls(body,button,ui,current,message) {
  const url=el('input');url.type='text';url.readOnly=true;url.setAttribute('aria-label','Download link');url.hidden=true;body.append(url);
  const copy=button('Copy link');copy.disabled=true;
  copy.addEventListener('click',async()=>{if(!current()||!url.value)return;try{await ui.copyToClipboard(url.value);if(current())message.textContent='Link copied.';}catch(_){if(current())message.textContent='Copy unavailable; select the link to copy it.';}});
  return value=>{const safe=safeUrl(value);if(!safe)throw new Error('Link result unavailable. Open Manage shared downloads.');url.value=safe;url.hidden=false;copy.disabled=false;};
}
function currentSource(entry,resource) {
  return resource && resource.provider==='host' && resource.kind==='file' && (resource.id || resource.resource_id)===entry.resource_id
    && Boolean(resource.ref || resource.resource_ref) && same(resource.revision,entry.revision)
    && resource.capabilities?.includes('stat') && resource.capabilities?.includes('download');
}

/** One current Host file. The caller owns the captured selection/currentness fence. */
export async function openFilesShare({entry,client,current,ui,status=()=>{},fetchImpl=fetch,wire=wireDialog,fixedSnapshot=false}={}) {
  if (!entry || entry.provider!=='host' || entry.kind!=='file' || !entry.resource_ref || !entry.resource_id || !entry.revision || !current()) throw new Error('Choose one current downloadable Host file.');
  const ref=entry.resource_ref, id=entry.resource_id, revision=JSON.parse(JSON.stringify(entry.revision));
  const base=dialogBase('Share download',fixedSnapshot
    ? `Reviewed download: ${entry.name || 'File'}. Share this fixed copy with the audience you select. Public links require a configured public app URL.`
    : `File: ${entry.name || 'File'}. Links download this file; HTML is not hosted as a website. Linked dependencies are not included. Public links require a configured public app URL.`,wire);
  const {dialog,body,message,button}=base;
  const mode=select('Share mode',[['snapshot','Snapshot (fixed copy)'],['live','Live (current file)']]);
  const audience=select('Audience',[['owner','Owner only'],['public','Anyone with link']]);
  const expiry=el('input');expiry.type='number';expiry.min='1';expiry.max='8760';expiry.placeholder='Default';expiry.setAttribute('aria-label','Expiry in hours');
  if (!fixedSnapshot) body.append(label('Mode ',mode));
  body.append(label('Audience ',audience),label('Expiry in hours ',expiry));
  const create=button('Create link'),showResult=resultControls(body,button,ui,current,message),close=button('Close');
  let closed=false,issued=false;const done=()=>{closed=true;if(dialog.open)dialog.close();else dialog.remove();};
  close.addEventListener('click',done);dialog.addEventListener('close',()=>{closed=true;},{once:true});
  watchScope(dialog,current,()=>{if(issued)status('Share may have completed. Open Manage shared downloads.',true);done();});
  create.addEventListener('click',async()=>{
    if(closed||issued)return;
    try {
      if(!current())throw new Error('The Files selection changed. Reopen Share.');
      const selected=options(audience,expiry), response=await client.stat(ref), fresh=response?.resource||response;
      if(!current()||!currentSource({resource_id:id,resource_ref:ref,revision},fresh))throw new Error('The selected file changed. Reopen Share.');
      const body={mode:fixedSnapshot ? 'snapshot' : mode.value,...selected,resource:{resource_ref:ref,expected_revision:revision},dependencies:[]};
      if(!['snapshot','live'].includes(body.mode))throw new Error('Choose a share mode.');
      issued=true;create.disabled=true;message.textContent='Creating link…';
      let result;try {result=await request(fetchImpl,'/api/files/share',{method:'POST',body});}
      catch(error){if(error.unknown || !(error instanceof Error) || !error.message.includes('not accepted'))throw new Error('The outcome may be unknown. Open Manage shared downloads; do not repeat this request.');throw error;}
      if(!current()||closed){status('Share may have completed. Open Manage shared downloads; do not repeat the request.',true);return;}
      const afterResponse=await client.stat(ref), after=afterResponse?.resource||afterResponse;
      if(!current()||closed||!currentSource({resource_id:id,resource_ref:ref,revision},after)
          || result.target?.resource_id!==id || !same(result.target?.revision,revision)) throw new Error('The file or selection changed. Open Manage shared downloads to review the result.');
      showResult(result.download_url);message.textContent='Link created. Copy it now; this link is shown once.';
    } catch(error) {const detail=issued && !String(error?.message || '').includes('not accepted') ? 'Share may have completed. Open Manage shared downloads; do not repeat the request.' : (error?.message || 'Share unavailable.');if(!closed)message.textContent=detail;else if(issued)status(detail,true);}
  });
  dialog.showModal();return dialog;
}

/** Explicit, fixed supported-text review; the caller owns original chat scope. */
export async function openChatSnapshotShare({chatID,current,ui,status=()=>{},fetchImpl=fetch,wire=wireDialog}={}) {
  if(typeof chatID!=='string'||!chatID||!current())throw new Error('Open a current chat first.');
  const base=dialogBase('Share supported chat text','This fixed text snapshot includes user-written sensitive text. Unsupported, mixed or active chats are unavailable. Later edits do not change this copy; deleting or withdrawing the source disables its links.',wire);
  const {dialog,body,message,button}=base, audience=select('Audience',[['owner','Owner only'],['public','Anyone with link']]);
  const expiry=el('input');expiry.type='number';expiry.min='1';expiry.max='8760';expiry.placeholder='Default';expiry.setAttribute('aria-label','Expiry in hours');
  const text=el('pre');text.setAttribute('aria-label','Text to share');text.style.whiteSpace='pre-wrap';body.append(text,label('Audience ',audience),label('Expiry in hours ',expiry));
  const create=button('Create link'),showResult=resultControls(body,button,ui,current,message),manage=button('Manage shared downloads'),close=button('Close');
  let closed=false,issued=false,settled=false,review=null;create.disabled=true;
  const prepareAbort=new AbortController();
  const done=()=>{closed=true;prepareAbort.abort();if(dialog.open)dialog.close();else dialog.remove();};
  close.addEventListener('click',done);dialog.addEventListener('close',()=>{closed=true;prepareAbort.abort();},{once:true});
  watchScope(dialog,current,()=>{if(issued&&!settled)status('Share may have completed. Open Manage shared downloads; do not repeat.',true);done();});
  manage.addEventListener('click',()=>{if(!current())return;done();void openFilesShareManager({current,ui,status,fetchImpl,wire});});
  create.addEventListener('click',async()=>{
    if(closed||issued||!current()||!review)return;
    try {
      const selected=options(audience,expiry);issued=true;create.disabled=true;message.textContent='Creating link…';
      const result=await request(fetchImpl,'/api/files/chat-snapshots',{method:'POST',body:{chat_id:chatID,expected_review_sha256:review,...selected}});
      if(closed||!current()){status('Share may have completed. Open Manage shared downloads; do not repeat.',true);return;}
      showResult(result.download_url);settled=true;message.textContent='Link created. Copy it now; this link is shown once.';
    } catch(error) {
      const rejected=issued && error?.unknown===false;
      if(rejected)settled=true;
      const detail=rejected?'The request was not accepted. Reopen to review the chat again.':issued?'Link outcome may be unknown. Open Manage shared downloads; do not repeat.':String(error?.message||'Share unavailable.');
      if(!closed&&current())message.textContent=detail;else if(issued)status(detail,true);
    }
  });
  dialog.showModal();message.textContent='Preparing supported text…';
  try {
    const result=await request(fetchImpl,'/api/files/chat-snapshots/prepare',{method:'POST',body:{chat_id:chatID},signal:prepareAbort.signal});
    if(closed||!current())return;
    if(result.version!==1||result.chat_id!==chatID||typeof result.text!=='string'||!result.text||new TextEncoder().encode(result.text).length>2*1024*1024
      ||!(/^[0-9a-f]{64}$/).test(result.review_sha256)||!(/^[0-9a-f]{64}$/).test(result.source_sha256)||!(/^[0-9a-f]{64}$/).test(result.text_sha256))throw new Error('Supported chat text unavailable.');
    text.textContent=result.text;review=result.review_sha256;create.disabled=false;message.textContent='Review the exact text before creating a link.';
  } catch(_) {if(!closed&&current())message.textContent='Supported chat text is unavailable for this chat.';}
  return dialog;
}

/** Select an existing owner publication by label; never derive its ID from a Files ref. */
export async function openFilesShareManager({current,ui,status=()=>{},fetchImpl=fetch,wire=wireDialog}={}) {
  if(!current())throw new Error('Files scope changed.');
  const base=dialogBase('Manage shared downloads','Select one of your publications. Revoke breaks all links for that publication; downloaded copies remain independent. Public links require a configured public app URL.',wire);
  const {dialog,body,message,button}=base, publication=el('select');publication.setAttribute('aria-label','Publication');
  const audience=select('Audience',[['owner','Owner only'],['public','Anyone with link']]);
  const expiry=el('input');expiry.type='number';expiry.min='1';expiry.max='8760';expiry.placeholder='Default';expiry.setAttribute('aria-label','Expiry in hours');
  body.append(label('Publication ',publication),label('Audience ',audience),label('Expiry in hours ',expiry));
  const grant=button('Create new link'),revoke=button('Break all links'),showResult=resultControls(body,button,ui,current,message),close=button('Close');
  let closed=false,issued=false,confirmRevoke=false,rows=[];grant.disabled=revoke.disabled=true;
  const done=()=>{closed=true;if(dialog.open)dialog.close();else dialog.remove();};close.addEventListener('click',done);dialog.addEventListener('close',()=>{closed=true;},{once:true});
  watchScope(dialog,current,()=>{if(issued)status('Link operation may have completed. Inspect publications.',true);done();});
  publication.addEventListener('change',()=>{confirmRevoke=false;revoke.textContent='Break all links';});
  async function load(){
    try {const page=await request(fetchImpl,'/api/files/library?limit=200');if(closed||!current())return;
      if(!Array.isArray(page.files)||page.files.length>200)throw new Error('Publications unavailable.');
      rows=page.files.filter(row=>row && typeof row.id==='string' && /^[0-9a-f]{32}$/.test(row.id) && typeof row.filename==='string');
      for(const row of rows){const option=el('option',`${row.filename.slice(0,240)} · ${String(row.created_at||'').slice(0,32)} · ${(Array.isArray(row.audiences)?row.audiences:[]).filter(x=>x==='owner'||x==='public').join(', ') || 'no active links'}`);option.value=row.id;publication.append(option);}
      if(rows.length){publication.value=rows[0].id;grant.disabled=revoke.disabled=false;message.textContent=page.files.length===200?'Showing up to 200 publications.':'';}else message.textContent='No published downloads.';
    }catch(error){if(!closed&&current())message.textContent=error.message||'Publications unavailable.';}
  }
  const selected=()=>rows.find(row=>row.id===publication.value);
  grant.addEventListener('click',async()=>{
    if(closed||issued||!current()||!selected())return;
    try {const opts=options(audience,expiry), id=selected().id;issued=true;grant.disabled=revoke.disabled=true;message.textContent='Creating link…';
      let result;try{result=await request(fetchImpl,`/api/files/${encodeURIComponent(id)}/grants`,{method:'POST',body:opts});}catch(_){throw new Error('Link outcome may be unknown. Close and inspect publications; do not repeat the request.');}
      if(closed||!current()){status('Link may have been created. Inspect publications; do not repeat the request.',true);return;}
      showResult(result.download_url);message.textContent='Link created. Copy it now; this link is shown once.';
    }catch(error){if(!closed)message.textContent=error.message||'Link unavailable.';}
  });
  revoke.addEventListener('click',async()=>{
    if(closed||issued||!current()||!selected())return;
    if(!confirmRevoke){confirmRevoke=true;revoke.textContent='Confirm break all links';message.textContent='Confirm to revoke every link for this publication.';return;}
    const id=selected().id;issued=true;grant.disabled=revoke.disabled=true;message.textContent='Breaking links…';
    try{const result=await request(fetchImpl,`/api/files/${encodeURIComponent(id)}/revoke`,{method:'POST'});if(closed||!current()){status('Revocation may have completed. Inspect publications.',true);return;}if(!Number.isInteger(result.revoked)||result.revoked<0)throw new Error('Revocation outcome unknown.');message.textContent=`Broke ${result.revoked} link${result.revoked===1?'':'s'}.`;}
    catch(_){if(!closed)message.textContent='Revocation outcome may be unknown. Inspect publications; do not repeat automatically.';}
  });
  dialog.showModal();void load();return dialog;
}

/** Invite known users and manage roles for one captured Host file. */
export async function openFilesAccess({entry,client,current,sameHuman,onChanged=()=>{},status=()=>{},fetchImpl=fetch,wire=wireDialog}={}) {
  if(!entry || entry.provider!=='host' || entry.kind!=='file' || !entry.resource_ref || !entry.resource_id || !entry.revision || typeof sameHuman!=='function' || !current()) throw new Error('Choose one current Host file.');
  const ref=entry.resource_ref,id=entry.resource_id,revision=JSON.parse(JSON.stringify(entry.revision));
  const resource={resource_ref:ref,expected_revision:revision};
  const {dialog,body,message,button}=dialogBase('Invite / manage access',`File: ${entry.name || 'File'}. Inviting creates a private live shared target. Commenter feedback is available when its optional profile is installed. Editing depends on the file format and available editor.`,wire);
  const users=el('select');users.setAttribute('aria-label','Username');
  const role=select('Role',[['viewer','Viewer'],['editor','Editor (where supported)']]);
  const bindings=el('select');bindings.setAttribute('aria-label','Current access');
  const feedbackTargets=el('select');feedbackTargets.setAttribute('aria-label','Shared feedback target');
  body.append(label('User ',users),label('Role ',role),label('Current access ',bindings),label('Feedback target ',feedbackTargets));
  const invite=button('Invite'),revoke=button('Revoke access'),viewFeedback=button('View feedback'),close=button('Close');invite.disabled=revoke.disabled=viewFeedback.disabled=true;
  let closed=false,issued=false,rows=[],names=[],feedbackReady=false,targets=[];
  const valid=()=>!closed && current() && sameHuman();
  const done=()=>{closed=true;if(dialog.open)dialog.close();else dialog.remove();};
  close.addEventListener('click',done);dialog.addEventListener('close',()=>{closed=true;},{once:true});
  watchScope(dialog,()=>current() && sameHuman(),()=>{if(issued)status('Access may have changed. Reopen access to inspect; do not repeat automatically.',true);done();});
  async function source() {
    if(!valid())throw new Error('Files selection changed. Reopen access.');
    const result=await client.stat(ref),fresh=result?.resource||result;
    if(!valid() || !currentSource({resource_id:id,resource_ref:ref,revision},fresh))throw new Error('File or selection changed. Reopen access.');
  }
  async function load() {
    try {
      await source();if(!valid())return;
      const directory=await request(fetchImpl,'/api/model-shares');if(!valid())return;
      if(!Array.isArray(directory.share_users) || directory.share_users.length>10000)throw new Error('Known users unavailable.');
      names=[...new Set(directory.share_users.map(item=>item?.username).filter(name=>typeof name==='string' && name.length>0 && name.length<=256))];
      for(const name of names){const item=el('option',name);item.value=name;users.append(item);}
      await source();if(!valid())return;
      const page=await request(fetchImpl,'/api/files/share/roles/list',{method:'POST',body:{resource}});if(!valid())return;
      if(!Array.isArray(page.items)||page.items.length>100 || typeof page.partial!=='boolean')throw new Error('Access list unavailable.');
      try {const profile=await request(fetchImpl,'/api/files-v1/shared-feedback/profile');if(!valid())return;feedbackReady=profile.version===1 && profile.available===true;}catch(_){feedbackReady=false;}
      if(feedbackReady){const item=el('option','Commenter');item.value='commenter';role.append(item);
        const selected=await request(fetchImpl,'/api/files/share/feedback-targets',{method:'POST',body:{resource}});if(!valid())return;
        if(selected.version!==1 || !Array.isArray(selected.items) || typeof selected.partial!=='boolean')throw Error('Feedback targets unavailable.');
        targets=selected.items.filter(item=>item && /^[a-f0-9]{32}$/.test(item.id||''));
        for(const [index,item] of targets.entries()){const option=el('option',`${item.name || 'Shared file'} · ${index+1}`);option.value=item.id;feedbackTargets.append(option);}
        viewFeedback.disabled=!targets.length;
      }
      rows=page.items.filter(row=>row && typeof row.binding_id==='string' && /^[A-Za-z0-9_-]{1,256}$/.test(row.binding_id) && typeof row.recipient_username==='string' && row.recipient_username.length<=256 && ['viewer','commenter','editor'].includes(row.role));
      for(const row of rows){const item=el('option',`${row.recipient_username} · ${row.role}${row.role==='commenter' && !feedbackReady?' (feedback setup unavailable)':''}`);item.value=row.binding_id;bindings.append(item);}
      invite.disabled=!names.length;revoke.disabled=!rows.length;
      message.textContent=page.partial || rows.length!==page.items.length?'Some access entries could not be shown.':'Select a user to invite or an access entry to revoke.';
    }catch(error){if(valid())message.textContent=error.message||'Access unavailable.';}
  }
  invite.addEventListener('click',async()=>{
    if(!valid()||issued||!names.includes(users.value)||!(['viewer','editor'].includes(role.value) || feedbackReady && role.value==='commenter'))return;
    const username=users.value,selectedRole=role.value;
    try {
      await source();if(!valid())return;
      issued=true;invite.disabled=revoke.disabled=true;message.textContent='Creating private live target…';
      const publication=await request(fetchImpl,'/api/files/share',{method:'POST',body:{mode:'live',audience:'owner',expires_in_hours:null,resource,dependencies:[]}});
      if(!valid()||publication.target?.mode!=='live'||publication.target?.resource_id!==id||!same(publication.target?.revision,revision))throw new Error('Target outcome needs review.');
      await source();if(!valid())throw new Error('Target outcome needs review.');
      message.textContent='Inviting user…';
      const result=await request(fetchImpl,'/api/files/share/roles',{method:'POST',body:{resource,recipient_username:username,role:selectedRole}});
      if(!sameHuman()){status('Invite may have completed. Reopen access to inspect; do not repeat automatically.',true);return;}
      if(result.version!==1 || result.resource_id!==id || result.role!==selectedRole || typeof result.binding_id!=='string')throw new Error('Invite outcome needs review.');
      done();status('Invitation created.');onChanged();
    }catch(_){const detail=issued?'The target or invitation may have been created. Reopen access to inspect; do not repeat automatically.':'File or selection changed. Reopen access.';if(valid())message.textContent=detail;else if(issued)status(detail,true);}
  });
  viewFeedback.addEventListener('click',async()=>{
    const target=targets.find(item=>item.id===feedbackTargets.value);if(!valid()||!feedbackReady||!target)return;
    try {await source();if(!valid())return;const {openSharedFeedback}=await import('./editor/sharedFeedback.js');
      if(!valid())return;await openSharedFeedback({publicationID:target.id,expectedRevision:revision,canComment:false,isCurrent:()=>valid() && targets.includes(target)});
    }catch(error){if(valid())message.textContent=error.message || 'Shared feedback unavailable.';}
  });
  revoke.addEventListener('click',async()=>{
    const selected=rows.find(row=>row.binding_id===bindings.value);if(!valid()||issued||!selected)return;
    try {
      await source();if(!valid())return;issued=true;invite.disabled=revoke.disabled=true;message.textContent='Revoking access…';
      const result=await request(fetchImpl,`/api/files/share/roles/${encodeURIComponent(selected.binding_id)}/revoke`,{method:'POST'});
      if(!sameHuman()){status('Access may have changed. Reopen access to inspect; do not repeat automatically.',true);return;}
      if(result.version!==1||result.binding_id!==selected.binding_id||result.state!=='revoked')throw new Error('Revocation outcome needs review.');
      done();status('Access revoked.');onChanged();
    }catch(_){const detail=issued?'Revocation may have completed. Reopen access to inspect; do not repeat automatically.':'File or selection changed. Reopen access.';if(valid())message.textContent=detail;else if(issued)status(detail,true);}
  });
  dialog.showModal();void load();return dialog;
}
