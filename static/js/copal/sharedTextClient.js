// Private, transport-injected WB17 client. A selector is not authorization.
const MAX_BYTES=256*1024, MAX_INSERT=256*1024, MAX_EDITS=64;
const encoder=new TextEncoder();
function validText(value, max=MAX_BYTES) {
  if(typeof value!=='string'||value.includes('\0')||value.includes('\r')||value.startsWith('\ufeff'))return false;
  for(let i=0;i<value.length;i++){
    const c=value.charCodeAt(i);
    if(c>=0xd800&&c<=0xdbff){if(i+1>=value.length||value.charCodeAt(i+1)<0xdc00||value.charCodeAt(i+1)>0xdfff)return false;i++;}
    else if(c>=0xdc00&&c<=0xdfff)return false;
  }
  return encoder.encode(value).length<=max;
}
function cleanString(value,max=256){return typeof value==='string'&&value.length>0&&encoder.encode(value).length<=max&&!/[\u0000-\u001f]/.test(value)}
async function sha(value){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',value))].map(x=>x.toString(16).padStart(2,'0')).join('')}
export async function sharedTextFingerprint(text){if(!validText(text))throw Error('Unsupported shared text.');const bytes=encoder.encode(text);return {sha256:await sha(bytes),size:bytes.length};}
export async function publicEditDigest(value) {
  if(!value||Object.keys(value).sort().join(',')!=='base_sha256,edits,operation_id,publication_id,version'||value.version!==1
    ||!cleanString(value.operation_id)||!cleanString(value.publication_id)||!/^[0-9a-f]{64}$/.test(value.base_sha256||'')
    ||!Array.isArray(value.edits)||value.edits.length<1||value.edits.length>MAX_EDITS)throw Error('Invalid shared edit.');
  let prior=-1,priorInsertion=false,total=0;
  const edits=value.edits.map(edit=>{
    if(!edit||Object.keys(edit).sort().join(',')!=='end,start,text'||!Number.isSafeInteger(edit.start)||!Number.isSafeInteger(edit.end)
      ||edit.start<0||edit.end<edit.start||edit.end>MAX_BYTES||edit.start<prior||(edit.start===prior&&edit.start===edit.end&&priorInsertion)
      ||!validText(edit.text,MAX_INSERT))throw Error('Invalid shared edit.');
    total+=encoder.encode(edit.text).length;if(total>MAX_INSERT)throw Error('Shared edit exceeds bounds.');
    prior=edit.end;priorInsertion=edit.start===edit.end;
    return {end:edit.end,start:edit.start,text:edit.text};
  });
  const canonical={base_sha256:value.base_sha256,edits,operation_id:value.operation_id,publication_id:value.publication_id,version:1};
  return sha(encoder.encode('openclank.collaboration.public.v1\0'+JSON.stringify(canonical)));
}
export async function publicWorkbookDigest(value) {
  if(!value||Object.keys(value).sort().join(',')!=='base_sha256,cells,operation_id,publication_id,version'||value.version!==1
    ||!cleanString(value.operation_id)||!cleanString(value.publication_id)||!/^[0-9a-f]{64}$/.test(value.base_sha256||'')
    ||!Array.isArray(value.cells)||value.cells.length<1||value.cells.length>64)throw Error('Invalid workbook edit.');
  const seen=new Set();let total=0;
  const cells=value.cells.map(cell=>{
    if(!cell||Object.keys(cell).sort().join(',')!=='cell_id,raw,sheet_id'
      ||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(cell.cell_id||'')||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(cell.sheet_id||'')
      ||typeof cell.raw!=='string'||cell.raw.length>65536||!cell.raw.isWellFormed())throw Error('Invalid workbook edit.');
    const key=cell.sheet_id+'\0'+cell.cell_id;if(seen.has(key))throw Error('Duplicate workbook cell.');seen.add(key);
    total+=encoder.encode(JSON.stringify(cell.raw)).length;if(total>MAX_INSERT)throw Error('Workbook edit exceeds bounds.');
    return {cell_id:cell.cell_id,raw:cell.raw,sheet_id:cell.sheet_id};
  });
  const canonical={base_sha256:value.base_sha256,cells,operation_id:value.operation_id,publication_id:value.publication_id,version:1};
  return sha(encoder.encode('openclank.collaboration.workbook.public.v1\0'+JSON.stringify(canonical)));
}
export async function publicSlidesDigest(value) {
  if(!value||Object.keys(value).sort().join(',')!=='base_sha256,objects,operation_id,publication_id,version'||value.version!==1
    ||!cleanString(value.operation_id)||!cleanString(value.publication_id)||!/^[0-9a-f]{64}$/.test(value.base_sha256||'')
    ||!Array.isArray(value.objects)||value.objects.length<1||value.objects.length>64)throw Error('Invalid slide edit.');
  const seen=new Set();let total=0;
  const objects=value.objects.map(object=>{
    if(!object||Object.keys(object).sort().join(',')!=='object_id,slide_id,text'
      ||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(object.slide_id||'')||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(object.object_id||'')
      ||typeof object.text!=='string'||object.text.length>65536||!object.text.isWellFormed())throw Error('Invalid slide edit.');
    const key=object.slide_id+'\0'+object.object_id;if(seen.has(key))throw Error('Duplicate slide object.');seen.add(key);
    total+=encoder.encode(JSON.stringify(object.text)).length;if(total>MAX_INSERT)throw Error('Slide edit exceeds bounds.');
    return {object_id:object.object_id,slide_id:object.slide_id,text:object.text};
  });
  const canonical={base_sha256:value.base_sha256,objects,operation_id:value.operation_id,publication_id:value.publication_id,version:1};
  return sha(encoder.encode('openclank.collaboration.slides.public.v1\0'+JSON.stringify(canonical)));
}
export function createSharedTextClient({accountID,publicationID,getScope,exchange,format='text'}) {
  if(!cleanString(accountID)||!cleanString(publicationID)||!['text','workbook','slides'].includes(format)||typeof getScope!=='function'||typeof exchange!=='function')throw Error('Shared text scope unavailable.');
  function ticket(){const scope=getScope();if(!scope||scope.accountID!==accountID||scope.publicationID!==publicationID||!Number.isSafeInteger(scope.epoch))throw Error('Shared text scope changed.');return {accountID,publicationID,epoch:scope.epoch};}
  function check(before){const now=ticket();if(now.epoch!==before.epoch)throw Error('Shared text scope changed.');}
  async function call(phase,body,before){check(before);const result=await exchange(phase,body);check(before);if(!result||typeof result!=='object'||Array.isArray(result))throw Error('Shared text response unavailable.');return result;}
  return Object.freeze({
    async read(){
      const before=ticket();const result=await call('read',{version:1,publication_id:publicationID},before);
      if(result.version!==1||!/^resource-[0-9a-f]{64}$/.test(result.resource_id||'')||!['viewer','commenter','editor'].includes(result.role)
        ||result.revision?.kind!=='hostFingerprint'||typeof result.revision.value!=='string'||typeof result.text!=='string')throw Error('Shared text response unavailable.');
      const fingerprint=await sharedTextFingerprint(result.text);check(before);
      if(result.revision.value!==`sha256:${fingerprint.sha256}:${fingerprint.size}`)throw Error('Shared text response unavailable.');
      return result;
    },
    async issue({operationID,baseText,baseSHA256,edits,cells,objects},beforeIssue){
      const before=ticket();const fingerprint=await sharedTextFingerprint(baseText);check(before);
      if(fingerprint.sha256!==baseSHA256)throw Error('Shared base changed.');
      const identity={version:1,publication_id:publicationID,operation_id:operationID,base_sha256:baseSHA256,...(format==='workbook'?{cells}:format==='slides'?{objects}:{edits})};
      const clientDigest=await (format==='workbook'?publicWorkbookDigest(identity):format==='slides'?publicSlidesDigest(identity):publicEditDigest(identity));check(before);
      const selector=Object.freeze({publication_id:publicationID,operation_id:operationID,client_digest:clientDigest});
      if(typeof beforeIssue!=='function')throw Error('Shared operation custody unavailable.');
      beforeIssue(selector);
      const result=await call('commit',{...identity,base_text:baseText,client_digest:clientDigest},before);
      if(result.version!==1||!['pending','unknown','complete','conflict','unavailable'].includes(result.state)||result.operation_id!==operationID||result.client_digest!==clientDigest)throw Error('Shared receipt unavailable.');
      return {selector,result};
    },
    async status(selector){const before=ticket();if(!selector||Object.keys(selector).sort().join(',')!=='client_digest,operation_id,publication_id'
      ||selector.publication_id!==publicationID||!cleanString(selector.operation_id)||!/^[0-9a-f]{64}$/.test(selector.client_digest||''))throw Error('Shared selector unavailable.');
      const result=await call('status',{version:1,...selector},before);
      if(result.version!==1||!['pending','unknown','complete','conflict','unavailable'].includes(result.state)||result.operation_id!==selector.operation_id||result.client_digest!==selector.client_digest)throw Error('Shared receipt unavailable.');return result;},
  });
}
