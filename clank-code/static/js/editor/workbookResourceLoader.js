// Files owns ref admission and source bytes; this only validates one bounded snapshot.
import {parseWorkbook} from './workbookModel.js';
const MAX_BYTES=8*1024*1024, ID=/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const same=(a,b)=>a && b && a.kind===b.kind && a.value===b.value;
function workbook(resource,resourceId,revision,policyGeneration) {
  const f=resource?.format;
  return resource?.provider==='host' && resource?.id===resourceId && resource?.kind==='file'
    && !!resource.ref && same(resource.revision,revision) && resource.policy_generation===policyGeneration
    && f?.version===1 && f.variant==='openclank-workbook-v1' && f.resourceRef===resource.ref
    && same(f.providerRevision,revision) && f.policyGeneration===policyGeneration
    && f.operations?.workbookView?.available===true && f.operations.workbookView.requires==='workbook-v1'
    && resource.capabilities?.includes('stat') && resource.capabilities?.includes('open');
}
export function workbookEmbedSelector(fragment,book) {
  if(fragment==='') return {kind:'sheet',sheetId:book.sheets[0].id};
  const match=/^(sheet|range|chart)=([A-Za-z0-9][A-Za-z0-9_-]{0,127})$/.exec(fragment);
  if(!match || !ID.test(match[2])) return null;
  return match[1]==='sheet'?{kind:'sheet',sheetId:match[2]}:match[1]==='range'?{kind:'range',rangeId:match[2]}:{kind:'chart',chartId:match[2]};
}
export async function loadWorkbookSnapshot({client,resourceRef,resourceId,revision,policyGeneration,accountId,fragment='',isCurrent,signal}={}) {
  if(!client || typeof client.stat!=='function' || typeof client.openResource!=='function' || !resourceRef || !resourceId || !revision
      || !Number.isSafeInteger(policyGeneration) || policyGeneration<0 || !accountId || typeof isCurrent!=='function') throw new TypeError('Workbook source unavailable');
  const check=()=>{if(signal?.aborted || isCurrent()!==true)throw new Error('Workbook scope changed');};
  check();const first=await client.stat(resourceRef,{signal});check();
  const before=first?.resource||first;
  if(!workbook(before,resourceId,revision,policyGeneration) || !Number.isSafeInteger(before.size) || before.size<0 || before.size>MAX_BYTES)throw new Error('Workbook source unavailable or too large');
  const response=await client.openResource(resourceRef,{expectedRevision:revision,signal});check();
  const handle=response?.payload?.resource, content=response?.payload?.text;
  if(!workbook(response?.resource,resourceId,revision,policyGeneration) || handle?.key?.provider!=='host'
      || handle?.key?.resourceId!==resourceId || handle?.key?.accountId!==accountId || !same(handle.revision,revision)
      || typeof content!=='string' || content.length>MAX_BYTES || new TextEncoder().encode(content).length>MAX_BYTES)throw new Error('Workbook snapshot changed');
  const book=parseWorkbook(content),selector=workbookEmbedSelector(fragment,book);
  if(!selector)throw new Error('Unsupported workbook fragment');
  const last=await client.stat(resourceRef,{signal});check();const after=last?.resource||last;
  if(!workbook(after,resourceId,revision,policyGeneration))throw new Error('Workbook source changed');
  return {source:content,resourceRef,resourceId,revision:{...revision},selector};
}
