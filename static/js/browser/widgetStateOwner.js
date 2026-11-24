import {openWidgetStateDialog,saveWidgetCopy,checkWidgetCopy,createWidgetFeedbackGuard} from './widgetState.js';
import {captureFeedbackScope} from '../editor/feedbackScope.js';
import {getCurrentSessionId} from '../sessions.js';
import {getWorkspaceId} from '../workspace.js';
import {filesFacadeClient} from '../filesFacadeClient.js';
import {createResourcePicker} from '../copal/resourcePicker.js';

// The existing Browser receipt slot is shared by both preview surfaces.
const receiptState={pending:null};
const scope=value=>String(value || 'default');
const announce=()=>document.dispatchEvent(new Event('openclank:widget-receipt-changed'));
export function hasPendingWidgetReceipt(){return Boolean(receiptState.pending);}
export async function checkWidgetReceipt() {
  const account=String(document.body?.dataset?.accountId || ''), workspace=scope(getWorkspaceId());
  const same=()=>String(document.body?.dataset?.accountId || '')===account && scope(getWorkspaceId())===workspace;
  const files=await import('../files.js'); if(!same()) throw Error('Account or workspace changed.');
  const result=await checkWidgetCopy({state:receiptState,account,workspace,isCurrent:same,reconcile:files.reconcileBrowserReportImport});
  if(!same()) throw Error('Account or workspace changed.'); announce(); return result;
}
export function openOwnedWidgetState({mount,model,isCurrent}) {
  const session=mount?.session, chat=String(getCurrentSessionId() || '');
  const current=()=>Boolean(isCurrent() && session?.session_id===mount?.session?.session_id && model?.isCurrent?.() !== false && String(document.body?.dataset?.accountId || '')===model?.accountScope);
  if (!current() || !session?.widget_state) throw Error('No source-declared widget state is available.');
  mount.widgetState.declaration();
  return openWidgetStateDialog({widget:mount.widgetState,isCurrent:current,
    async loadFiles() {
      const account=model.accountScope, workspace=scope(model.workspaceScope);
      const chosen=await new Promise(resolve=>{let selected=null;const picker=createResourcePicker({client:filesFacadeClient,purpose:'file',accountScope:account,workspaceScope:workspace,
        getAccountScope:()=>String(document.body?.dataset?.accountId || ''),getWorkspaceScope:()=>scope(getWorkspaceId()),isOriginCurrent:current,
        rootLabel:'Choose widget state copy',isCompatible:r=>/\.json$/i.test(r.name || '') ? '' : 'Choose a JSON file.',
        onSelect:r=>{if(typeof r.revision?.kind!=='string' || !r.revision.kind || typeof r.revision?.value!=='string' || !r.revision.value)throw Error('Choose a revisioned JSON file.');selected=r;return true;},onClose:cancelled=>resolve(cancelled?null:selected)});void picker.open();});
      if(!chosen)return null;if(!current())throw Error('Preview changed.');
      const read=async()=>{const result=await filesFacadeClient.openResource(chosen.ref,{expectedRevision:chosen.revision});
        const text=result?.payload?.text, resource=result?.resource;
        if(!current()||typeof text!=='string'||new TextEncoder().encode(text).length>16384||resource?.id!==chosen.resourceId)throw Error('State copy changed or is unavailable.');return text;};
      const text=await read();return {text,verify:async()=>{if(await read()!==text)throw Error('State copy changed.');}};
    },
    async save(file,selectionCurrent) {
      if(!current() || !selectionCurrent()) throw Error('Preview or selection changed.');
      const files=await import('../files.js');if(!current() || !selectionCurrent()) throw Error('Preview or selection changed.');
      try {return await saveWidgetCopy({state:receiptState,binding:{account:model.accountScope,workspace:scope(model.workspaceScope),session:session.session_id},isCurrent:()=>current()&&selectionCurrent(),verify:()=>mount.widgetState.verify(),file,importFile:files.importBrowserReportFile});}
      finally {announce();}
    },
    async feedback(rows,fields,selectionCurrent) {
      const guard=createWidgetFeedbackGuard(()=>current()&&chat&&String(getCurrentSessionId() || '')===chat,selectionCurrent), stillCurrent=guard.current;
      if(!stillCurrent()) throw Error('Original chat or preview changed.');
      const witness=await captureFeedbackScope(chat,stillCurrent);if(!stillCurrent())throw Error('Original chat or preview changed.');
      await mount.widgetState.verify();if(!stillCurrent())throw Error('Original chat or preview changed.');
      const text='Widget-reported state (unverified):\n'+rows.map(row=>`${fields.find(f=>f.id===row.id)?.label || row.id} [${row.id}]: ${JSON.stringify(row.value)}`).join('\n');
      if(text.length>3500)throw Error('Selected state is too large for a feedback draft.');
      const detail={chat,target:{resourceRef:model.resourceRef,expectedRevision:model.revision,anchor:{kind:'whole'},scope:witness},label:'Widget-reported state · unverified',text,isCurrent:stillCurrent,accepted:false};
      document.dispatchEvent(new CustomEvent('openclank:widget-feedback-draft',{detail}));
      if(!detail.accepted || !guard.accept())throw Error('Open the original chat feedback panel and clear its existing draft first.');
    }});
}
