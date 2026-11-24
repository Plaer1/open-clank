import { wireDialog } from '../copal/overlays.js';
import { openFilesShare } from '../filesShare.js';
import ui from '../ui.js';

const MAX_BYTES = 2 * 1024 * 1024;
const privateReferences = /(?:clank:\/\/|oc-history-action:v[12]:|\/api\/(?:session|history|copal|files-v1)\/|\/chat(?:\/|[?#]))/i;
const element = (tag, text) => Object.assign(document.createElement(tag), text == null ? {} : {textContent:text});
const sameRevision = (a,b) => Boolean(a && b && a.kind === b.kind && a.value === b.value);

export function openStandaloneTextShare({draft, client, current, parentRef, chooseParent, status}) {
  if (!current() || (!parentRef && typeof chooseParent !== 'function')) throw new Error('Open an authorized writable Host folder first.');
  const dialog=element('dialog'); dialog.className='copal-dialog';
  const message=element('p'); message.setAttribute('role','status');
  const title=element('h2','Share standalone text');
  const intro=element('p',chooseParent
    ? 'Write or paste the text you want to share. Edit or redact it, then preview the exact plain-text download.'
    : 'Choose the text to send. Edit or redact it, then preview the exact plain-text download.');
  const name=element('input'), nameLabel=element('label','Download file name');
  name.value=draft.name; name.maxLength=240; nameLabel.append(name);
  const source=element('textarea'), sourceLabel=element('label','Standalone text');
  source.rows=12; source.value=draft.text; sourceLabel.append(source);
  const preview=element('pre'); preview.hidden=true; preview.style.whiteSpace='pre-wrap';
  preview.setAttribute('aria-label','Exact text the recipient will receive');
  const footer=element('footer'); footer.className='copal-dialog-actions';
  function button(label, run) {
    const node=element('button',label); node.type='button'; node.className='copal-btn';
    node.addEventListener('click',run); footer.append(node); return node;
  }
  let reviewed=null;
  const alive=()=>current() && dialog.isConnected && dialog.open;
  const changed=()=>{
    draft.name=name.value; draft.text=source.value; reviewed=null; preview.hidden=true;
    save.disabled=true; share.disabled=true; message.textContent='';
  };
  name.addEventListener('input',changed); source.addEventListener('input',changed);
  button('Preview',()=>{
    if (!alive()) return;
    try {
      const filename=name.value.trim(), text=source.value;
      const bytes=new TextEncoder().encode(text).length;
      if (!filename.toLowerCase().endsWith('.txt') || filename.length > 240 || /[\/\u0000-\u001f\u007f]/.test(filename) ||
          filename.includes(String.fromCharCode(92))) throw new Error('Use a plain-text file name ending in .txt.');
      if (!text.trim() || bytes > MAX_BYTES) throw new Error('Choose nonempty text up to 2 MiB.');
      if (privateReferences.test(text)) throw new Error('Remove private app links and recorded change references before sharing.');
      if (draft.attempt && (draft.attempt.name !== filename || draft.attempt.text !== text)) throw new Error('Check the original private-copy request before changing its text.');
      reviewed={filename,text};
      preview.textContent=text; preview.hidden=false;
      share.disabled=!draft.saved || draft.saved.name !== filename || draft.saved.text !== text;
      save.disabled=!share.disabled;
      message.textContent=share.disabled ? 'Review this exact text before saving a private copy.' : 'The reviewed private copy is ready to share.';
    } catch (error) { message.textContent=error.message; }
  });
  const save=button(draft.attempt ? 'Check private copy' : 'Save private copy',async()=>{
    if (!alive() || !reviewed || draft.busy) return;
    const captured=reviewed;
    draft.busy=true; save.disabled=true; share.disabled=true; name.disabled=true; source.disabled=true;
    try {
      let receipt, attempt=draft.attempt;
      if (attempt) receipt=await client.operationReceipt(attempt.id,{generation:attempt.generation});
      else {
        const destination=parentRef || await chooseParent();
        if (!alive() || reviewed !== captured) return;
        if (!destination) { message.textContent='Choose a writable Host folder to keep a private copy.'; return; }
        const stat=await client.stat(destination), parent=stat?.resource || stat;
        if (!alive() || reviewed !== captured || parent?.provider !== 'host' || parent.kind !== 'folder' ||
            !parent.ref || !parent.capabilities?.includes('write') || !Number.isSafeInteger(parent.policy_generation)) {
          throw new Error('The private destination is unavailable.');
        }
        attempt={id:'standalone-text-'+crypto.randomUUID(),generation:parent.policy_generation,name:captured.filename,text:captured.text};
        draft.attempt=attempt;
        try {
          receipt=await client.importFile(new File([attempt.text],attempt.name,{type:'text/plain'}),{
            operationId:attempt.id,itemId:attempt.id,generation:attempt.generation,destinationRef:parent.ref,name:attempt.name,collision:'fail',
          });
        } catch (error) {
          if (Number(error?.status) >= 400 && Number(error.status) < 500 &&
              ![408,429].includes(Number(error.status)) && error.code !== 'operation_pending') {
            draft.attempt=null; throw error;
          }
          throw new Error('Private copy outcome is unknown. Check the original request.');
        }
      }
      const item=receipt?.items?.find(entry=>entry.item_id === attempt.id);
      if (!item || item.outcome === 'pending') throw new Error('The private copy is pending. Check its original request.');
      if (!['committed','unchanged'].includes(item.outcome)) {
        draft.attempt=null;
        throw new Error(item.outcome === 'conflict' ? 'That file name already exists. Choose another.' : 'The private copy was not saved.');
      }
      draft.attempt=null;
      draft.saved=null;
      if (!item.resource_ref || item.revision?.kind !== 'hostFingerprint') throw new Error('The copy was saved in Files. Its exact revision could not be verified.');
      const opened=await client.openResource(item.resource_ref,{expectedRevision:item.revision});
      const row=opened?.resource;
      if (row?.provider !== 'host' || row.kind !== 'file' || !row.id || !row.ref ||
          !sameRevision(row.revision,item.revision) || opened.payload?.text !== attempt.text) throw new Error('The saved copy does not match the reviewed text.');
      draft.saved={name:attempt.name,text:attempt.text,entry:{name:attempt.name,provider:'host',kind:'file',
        resource_ref:row.ref,resource_id:row.id,revision:row.revision}};
      if (alive()) message.textContent='Private copy saved. Review the audience before sharing it.';
    } catch (error) {
      if (alive()) message.textContent=error.message || 'The private copy could not be verified.';
    } finally {
      draft.busy=false;
      if (alive()) {
        name.disabled=Boolean(draft.attempt); source.disabled=Boolean(draft.attempt);
        save.textContent=draft.attempt ? 'Check private copy' : 'Save private copy';
        share.disabled=!draft.saved || draft.saved.name !== reviewed?.filename || draft.saved.text !== reviewed?.text;
        save.disabled=!reviewed || !share.disabled;
      }
    }
  });
  save.disabled=true;
  const share=button('Share reviewed copy',async()=>{
    const saved=draft.saved;
    if (!alive() || !saved || !reviewed || saved.name !== reviewed.filename || saved.text !== reviewed.text) return;
    dialog.close();
    try { await openFilesShare({entry:saved.entry,client,current,ui,status,fixedSnapshot:true}); }
    catch (error) { if (current()) status(error.message || 'Share unavailable.',true); }
  });
  share.disabled=true;
  button('Close',()=>dialog.close());
  if (draft.attempt) { name.disabled=true; source.disabled=true; }
  dialog.append(title,intro,nameLabel,sourceLabel,preview,message,footer);
  const scopeEvents=['odysseus:session-selected','openclank:auth-context-changed','openclank-account-changed',
    'workspace-change','chat-workspace-change','openclank-window-closed'];
  const closeIfStale=()=>{ if (dialog.open && !current()) dialog.close(); };
  const scopeListeners=new AbortController();
  for (const target of [document,window]) {
    for (const type of scopeEvents) target.addEventListener(type,closeIfStale,{signal:scopeListeners.signal});
  }
  dialog.addEventListener('close',()=>scopeListeners.abort(),{once:true});
  wireDialog(dialog); document.body.append(dialog); dialog.showModal();
  return dialog;
}
