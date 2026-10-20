// Narrow CAS append, deliberately separate from History draft restoration.
export function codeEvidenceCard(evidence) {
  const receipt=evidence?.receipt, source=evidence?.source, span=source?.span;
  if(evidence?.schema!=='open-clank.code-resource/v1'||evidence.state!=='current'||evidence.validation_kind!=='current_source'
    ||evidence.current_source_verified!==true||typeof evidence.excerpt!=='string'||!evidence.excerpt
    ||!receipt||receipt.workspace_id!==evidence.workspace_id||typeof receipt.checkout_id!=='string'
    ||!source||typeof source.rel_path!=='string'||typeof source.source_hash!=='string'||!span
    ||!Number.isSafeInteger(span.start_byte)||!Number.isSafeInteger(span.end_byte)||span.start_byte<0||span.end_byte<span.start_byte)
    return null;
  const card=`[Untrusted code evidence — observed source, not runtime behavior]\nWorkspace: ${receipt.workspace_id}\nCheckout: ${receipt.checkout_id}\nGeneration: ${receipt.head_generation}\nSource: ${source.rel_path}:${span.start_line??'?'}–${span.end_line??'?'} bytes ${span.start_byte}:${span.end_byte}\nHash: ${source.source_hash}\n${evidence.excerpt}\n[End code evidence]`;
  return new TextEncoder().encode(JSON.stringify({role:'user',content:card})).length<=2048?card:null;
}
export function appendCodeEvidenceCAS(evidence,{expected,read,commit,isCurrent=()=>true,ask=false,submit}) {
  const card=codeEvidenceCard(evidence);
  if(!card)return {ok:false,message:'Current cited evidence is unavailable or exceeds the 2048-byte card cap.'};
  const actual=read();
  if(!expected||!expected.sid||!expected.owner||!isCurrent()||['sid','owner','epoch','version','text','attachments','authEpoch'].some(key=>actual[key]!==expected[key])
    ||!Array.isArray(actual.pending)||actual.pending.length!==expected.pending.length||actual.pending.some((file,index)=>file!==expected.pending[index]))
    return {ok:false,message:'Chat, account or draft changed. Evidence was not added.'};
  commit(`${actual.text}${actual.text?'\n\n':''}${card}`);
  if(ask && isCurrent())submit?.();
  return {ok:true,message:ask?'Evidence added; explicit Ask submitted.':'Cited evidence appended. Attachments preserved.'};
}
