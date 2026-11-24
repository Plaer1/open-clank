// A separate, non-indexed, read-only view of imported static/trace evidence: never creates Code Graph nodes, edges or links.
export const MAX_INGEST_BYTES=1048576;
export const ADAPTER_ID='fixture_jsonl';
// The server lists only adapters with a real producer (none today); without one the overlay is not offered.
export const traceOverlayOffered=capabilities=>Array.isArray(capabilities?.ingestAdapters)&&capabilities.ingestAdapters.length>0&&capabilities.ingestAdapters.includes(ADAPTER_ID);
const ORIGIN={static:'static evidence',static_xref:'static evidence (xref)',runtime_observed:'runtime-observed'};
const STATE={
  current:'Recorded against the current source bytes (checked at read; not continuously monitored).',
  historical:'Warning: this evidence was recorded against different source bytes than the current source.',
  unchecked:'Not verified against the current source.'};
export const stateText=state=>STATE[state]||'Source state unknown.';
const hex=value=>Number.isSafeInteger(value)?`0x${value.toString(16)}`:String(value);
const ID=/^overlay:[a-f0-9]{64}$/;
// Every row carries a visible origin label; static and runtime rows are never mixed without it.
export function traceRows(value){
  if(value?.schema!=='openclank.trace-overlay.v1'||value.state!=='verified'||!Object.hasOwn(STATE,value.sourceState)||!['functions','edges','hits'].every(key=>Array.isArray(value[key])))throw new Error('Trace overlay unavailable.');
  const rows=[];
  for(const [list,kind] of [['functions','function'],['edges','edge'],['hits','hit']])for(const row of value[list]){
    if(!row||typeof row.id!=='string'||!ID.test(row.id)||!Object.hasOwn(ORIGIN,row.origin))throw new Error('Trace overlay unavailable.');
    const detail=Object.entries(row).filter(([key,field])=>!['id','origin','resolved'].includes(key)&&field!==undefined).map(([key,field])=>`${key}=${field===null?'unresolved':['address','from','to'].includes(key)?hex(field):String(field)}`).join(' ');
    rows.push({id:row.id,kind,label:ORIGIN[row.origin],text:`[${ORIGIN[row.origin]}] ${kind} ${detail}`});
  }
  return rows;
}
export function checkFile(file){
  if(!file||!Number.isSafeInteger(file.size)||file.size<1)throw new Error('Choose a non-empty .jsonl file.');
  if(file.size>MAX_INGEST_BYTES)throw new Error('File is larger than 1 MiB.');
  if(!/\.jsonl$/i.test(String(file.name||'')))throw new Error('Choose a .jsonl file.');
}
export function bytesToBase64(bytes){let text='';for(let i=0;i<bytes.length;i+=0x8000)text+=String.fromCharCode(...bytes.subarray(i,i+0x8000));return btoa(text);}

export function mountTraceOverlay({container,client,workspaceID,sourceRef,isCurrent=()=>true}){
  let disposed=false,controller=new AbortController();
  const current=()=>!disposed&&container.isConnected&&isCurrent();
  const title=document.createElement('h3');title.textContent='Imported trace evidence · derived, non-indexed (off until you import or open one)';
  const status=document.createElement('p');status.setAttribute('aria-live','polite');
  const stateLine=document.createElement('p'),list=document.createElement('ul');
  const picker=document.createElement('input');picker.type='file';picker.accept='.jsonl';picker.setAttribute('aria-label','Import one .jsonl evidence file');
  const idField=document.createElement('input');idField.type='text';idField.placeholder='ingest id';idField.setAttribute('aria-label','Open an existing ingest by id');
  const open=document.createElement('button');open.type='button';open.textContent='Open ingest';
  container.replaceChildren(title,picker,idField,open,status,stateLine,list);
  const show=async(ingestID)=>{
    const value=await client.analysisIngestOverlay({workspaceID,ingestID,sourceRef},{signal:controller.signal});if(!current())return;
    const rows=traceRows(value);
    stateLine.textContent=stateText(value.sourceState);
    list.replaceChildren(...rows.map(row=>{const item=document.createElement('li');item.textContent=row.text;return item;}));
    status.textContent=`Ingest ${ingestID}: ${rows.length} rows, ${value.unresolvedEdges} unresolved edges. No Code Graph nodes or edges are created.`;
  };
  const run=work=>async()=>{if(!current())return;status.textContent='Working…';try{await work();}catch(error){if(current()&&error?.name!=='AbortError'){stateLine.textContent='';list.replaceChildren();status.textContent=error?.message&&/^(Choose|File)/.test(error.message)?error.message:'Trace evidence is unavailable or the source changed.';}}};
  picker.addEventListener('change',run(async()=>{
    const file=picker.files?.[0];checkFile(file);
    const bytes=new Uint8Array(await file.arrayBuffer());if(bytes.length!==file.size)throw new Error('File changed while reading.');
    const preview=await client.previewAnalysisProject({workspaceID,sourceRef},{signal:controller.signal});if(!current())return;
    const revision=preview?.source?.revision;if(revision?.kind!=='hostFingerprint'||typeof preview.source.ref!=='string')throw new Error('The exact source preview is unavailable.');
    const saved=await client.saveAnalysisIngest({workspaceID,sourceRef:preview.source.ref,sourceRevision:revision,adapterID:ADAPTER_ID,data_b64:bytesToBase64(bytes)},{signal:controller.signal});if(!current())return;
    if(typeof saved?.ingestID!=='string')throw new Error('Trace evidence is unavailable.');
    idField.value=saved.ingestID;await show(saved.ingestID);
  }));
  open.addEventListener('click',run(async()=>{const id=idField.value.trim();if(!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id))throw new Error('Choose an ingest id.');await show(id);}));
  const dispose=()=>{if(disposed)return;disposed=true;controller.abort();container.replaceChildren();};
  return {dispose,isCurrent:current};
}
