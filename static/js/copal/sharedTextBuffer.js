// Save adapter for an existing ResourceBuffer; no separate draft or authority store.
import {sharedTextFingerprint} from './sharedTextClient.js';
import {parseWorkbook,serializeWorkbook} from '../editor/workbookModel.js';
import {parseSlideDeck,serializeSlideDeck} from '../editor/slideModel.js';
const encoder=new TextEncoder();
function revision(value){return value&&value.kind==='hostFingerprint'&&typeof value.value==='string'&&/^sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)$/.test(value.value);}
function codePointBoundary(text,index){return index===0||index===text.length||!(text.charCodeAt(index)>=0xdc00&&text.charCodeAt(index)<=0xdfff);}
function delta(before,after){
  let left=0,tail=0;const common=Math.min(before.length,after.length);
  while(left<common&&before[left]===after[left])left++;
  while(left&&!codePointBoundary(before,left))left--;
  while(tail<before.length-left&&tail<after.length-left&&before[before.length-tail-1]===after[after.length-tail-1])tail++;
  while(tail&&(!codePointBoundary(before,before.length-tail)||!codePointBoundary(after,after.length-tail)))tail--;
  return {start:encoder.encode(before.slice(0,left)).length,end:encoder.encode(before.slice(0,before.length-tail)).length,text:after.slice(left,after.length-tail)};
}
function workbookChanges(before,after){
  const base=parseWorkbook(before), draft=parseWorkbook(after), desired=new Map();
  for(const sheet of draft.sheets) for(const row of sheet.rows) for(const cell of row) desired.set(sheet.id+'\0'+cell.id,cell.raw);
  const cells=[];
  for(const sheet of base.sheets) for(const row of sheet.rows) for(const cell of row){
    const key=sheet.id+'\0'+cell.id;
    if(!desired.has(key))throw Error('Workbook structure changed; retain the draft.');
    const raw=desired.get(key);
    if(raw!==cell.raw){cells.push({sheet_id:sheet.id,cell_id:cell.id,raw});cell.raw=raw;}
  }
  if(!cells.length||cells.length>64||serializeWorkbook(base)!==after)throw Error('Only existing workbook cell edits can be saved; retain the draft.');
  return cells;
}
function slideChanges(before,after){
  const base=parseSlideDeck(before), draft=parseSlideDeck(after), desired=new Map();
  for(const slide of draft.slides) for(const object of slide.objects) if(object.type==='text') desired.set(slide.id+'\0'+object.id,object.text);
  const objects=[];
  for(const slide of base.slides) for(const object of slide.objects) if(object.type==='text'){
    const key=slide.id+'\0'+object.id;
    if(!desired.has(key))throw Error('Slide structure changed; retain the draft.');
    const text=desired.get(key);if(text!==object.text){objects.push({slide_id:slide.id,object_id:object.id,text});object.text=text;}
  }
  if(!objects.length||objects.length>64||serializeSlideDeck(base)!==after)throw Error('Only existing slide text edits can be saved; retain the draft.');
  return objects;
}
export function sharedTextHandle({accountID,publicationID,resourceID,revision:head,name,role}){
  if(!accountID||!publicationID||!/^resource-[0-9a-f]{64}$/.test(resourceID||'')||!revision(head)||!['viewer','commenter','editor'].includes(role))throw Error('Shared resource unavailable.');
  return {key:{accountId:accountID,workspaceId:'shared:'+publicationID,provider:'host',resourceId:resourceID},revision:head,
    locator:{displayName:String(name||'Shared text'),locationLabel:'Shared resource'},representation:'text',capabilities:{read:true,edit:role==='editor'}};
}
export function createSharedTextSaveAdapter({client,publicationID,initialText,initialRevision,role,format='text'}){
  if(!client||!publicationID||!revision(initialRevision)||role!=='editor'||!['text','workbook','slides'].includes(format))throw Error('Shared editing unavailable.');
  let baseText=initialText,baseRevision=initialRevision,pending=null,candidate=null;
  async function accepted(result,snapshot,{commit=true}={}){
    if(result?.state==='conflict'){if(commit)pending=null;return {outcome:'conflict',remote:result.current||null};}
    if(result?.state==='unavailable'){if(commit)pending=null;return {outcome:'failed',uncertain:false,retryable:false,message:'Shared edit was not applied.'};}
    if(result?.state!=='complete'||!revision(result.revision))return {outcome:'failed',uncertain:true,retryable:false,message:'Original shared edit needs status reconciliation.'};
    if(format==='workbook'||format==='slides'){try{(format==='workbook'?parseWorkbook:parseSlideDeck)(typeof result.text==='string'?result.text:result.current?.text)}catch{return {outcome:'failed',uncertain:true,retryable:false,message:'Current workbook needs a fresh authorized read.'};}}
    if(typeof result.text!=='string' && revision(result.current?.revision) && typeof result.current?.text==='string'){
      const current=await sharedTextFingerprint(result.current.text);
      if(result.current.revision.value!==`sha256:${current.sha256}:${current.size}`)return {outcome:'failed',uncertain:true,retryable:false,message:'Current shared text needs a fresh authorized read.'};
      if(commit){baseText=result.current.text;baseRevision=result.current.revision;pending=null;}
      return {outcome:'applied',revision:result.current.revision,committedRevision:result.revision,verifiedMergedSnapshot:true,forceSharedConflict:true,
        snapshot:{...snapshot,envelope:{...snapshot.envelope,text:result.current.text}}};
    }
    if(typeof result.text!=='string')return {outcome:'failed',uncertain:true,retryable:false,message:'Original shared edit needs status reconciliation.'};
    const actual=await sharedTextFingerprint(result.text);
    if(result.revision.value!==`sha256:${actual.sha256}:${actual.size}`)return {outcome:'failed',uncertain:true,retryable:false,message:'Committed text requires a fresh authorized read.'};
    if(commit){baseText=result.text;baseRevision=result.revision;pending=null;}
    return {outcome:'applied',revision:result.revision,
      ...(result.text!==snapshot.envelope.text ? {verifiedMergedSnapshot:true} : {}),
      snapshot:{...snapshot,envelope:{...snapshot.envelope,text:result.text}}};
  }
  return Object.freeze({
    async save(snapshot){
      if(pending)return {outcome:'failed',uncertain:true,retryable:false,message:'Check the original shared edit status first.'};
      if(!snapshot||typeof snapshot.actionId!=='string'||!snapshot.actionId||!revision(snapshot.expectedRevision)||snapshot.expectedRevision.value!==baseRevision.value||typeof snapshot.envelope?.text!=='string')return {outcome:'conflict',message:'Shared base changed; retain the draft.'};
      const after=snapshot.envelope.text;
      const beforeHash=await sharedTextFingerprint(baseText);await sharedTextFingerprint(after);
      if(baseRevision.value!==`sha256:${beforeHash.sha256}:${beforeHash.size}`)return {outcome:'conflict',message:'Shared base changed; retain the draft.'};
      if(after===baseText)return {outcome:'applied',revision:baseRevision,snapshot};
      try{
        const change=format==='workbook'?{cells:workbookChanges(baseText,after)}:format==='slides'?{objects:slideChanges(baseText,after)}:{edits:[delta(baseText,after)]};
        const response=await client.issue({operationID:snapshot.actionId,baseText,baseSHA256:beforeHash.sha256,...change},selector=>{pending={selector,snapshot};});
        return await accepted(response.result,snapshot);
      }catch(error){return {outcome:'failed',uncertain:Boolean(pending),retryable:!pending,message:pending?'Original shared edit outcome is unknown; check status.':String(error?.message||'Shared edit unavailable.')};}
    },
    adoptExternal(text, head){
      if(pending || candidate || !revision(head) || typeof text !== 'string') return false;
      if(format==='workbook'||format==='slides'){try{(format==='workbook'?parseWorkbook:parseSlideDeck)(text)}catch{return false;}}
      baseText=text; baseRevision=head; return true;
    },
    async reconcile(){
      if(!pending)return null;
      const captured=pending;const result=await client.status(captured.selector);if(pending!==captured)return null;
      const outcome=await accepted(result,captured.snapshot,{commit:false});
      if(pending!==captured)return null;
      if(['complete','conflict','unavailable'].includes(result.state) && outcome?.uncertain!==true){
        const verified={...outcome,verifiedOriginalReceipt:true,originalActionID:captured.selector.operation_id};
        candidate={captured,verified,state:result.state};return verified;
      }
      return outcome;
    },
    finalizeReconciliation(verified){
      if(!candidate||candidate.verified!==verified||pending!==candidate.captured)return false;
      if(candidate.state==='complete'){
        baseText=verified.snapshot.envelope.text;baseRevision=verified.revision;
      }
      pending=null;candidate=null;return true;
    },
    getPendingSelector(){return pending?{...pending.selector}:null;},
  });
}
