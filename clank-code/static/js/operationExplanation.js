// Presentation only: this module never grants or executes an operation.
export function exactOperationDetails(detail) {
  const source = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : {};
  const lines = [];
  const display = value => typeof value === 'string' ? value : JSON.stringify(value);
  if (Object.hasOwn(source,'command')) lines.push(`Command: ${display(source.command)}`);
  if (Object.hasOwn(source,'argv')) lines.push(`Argv: ${JSON.stringify(source.argv)}`);
  for (const [key, label] of [['filepath','File target'],['path','Path target'],['target','Target'],['resource','Resource'],['workdir','Working directory'],['cwd','Current directory']]) {
    if (Object.hasOwn(source,key)) lines.push(`${label}: ${display(source[key])}`);
  }
  return lines;
}

function canonicalMaterial(payload, sessionId) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) || payload.session_id !== sessionId ||
      !['request_id','session_id','turn_id','permission_type'].every(key => typeof payload[key] === 'string') ||
      !Number.isSafeInteger(payload.revision) || payload.revision < 0 || !payload.detail ||
      Object.getPrototypeOf(payload.detail) !== Object.prototype) return null;
  const material = [payload.request_id,payload.session_id,payload.turn_id,payload.revision,payload.permission_type,payload.detail];
  let entries=0, textBytes=0;
  const utf8 = new TextEncoder();
  const encode=(value,depth)=>{
    if (++entries>256 || depth>8) throw new Error('bound');
    if (value===null || typeof value==='boolean') return JSON.stringify(value);
    if (typeof value==='number' && Number.isSafeInteger(value)) return JSON.stringify(value);
    if (typeof value==='string') {
      if (value.length > 131072 || /[\uD800-\uDFFF]/u.test(value)) throw new Error('unicode or bound');
      let characters=0; for (const _ of value) if (++characters>65536) throw new Error('bound');
      textBytes+=utf8.encode(value).length; if (textBytes>65536) throw new Error('bound');
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return '['+value.map(item=>encode(item,depth+1)).join(',')+']';
    if (value && Object.getPrototypeOf(value)===Object.prototype) {
      const keys=Object.keys(value).sort();
      if (keys.some(key=>key.length>256 || !/^[\x20-\x7e]+$/.test(key))) throw new Error('key');
      return '{'+keys.map(key=>{ textBytes+=key.length; if(textBytes>65536) throw new Error('bound'); return JSON.stringify(key)+':'+encode(value[key],depth+1); }).join(',')+'}';
    }
    throw new Error('type');
  };
  try { const value=encode(material,0);return utf8.encode(value).length<=65536?value:null; } catch (_) { return null; }
}

export async function verifiedOperationExplanation(payload, sessionId) {
  const proof=payload?.explanation;
  const keys=['version','request_id','session_id','turn_id','revision','action_sha256','intent','destructive','network','privilege','recovery','grant'];
  const expectedIntent=payload?.permission_type==='edit' && typeof payload?.detail?.filepath==='string' && payload.detail.filepath ? 'requested_file_edit':'unknown';
  const validProof=()=>proof && typeof proof==='object' && !Array.isArray(proof) && Object.keys(proof).length===keys.length && keys.every(key=>Object.hasOwn(proof,key)) &&
    proof.version===1 && ['request_id','session_id','turn_id','revision'].every(key=>proof[key]===payload[key]) &&
    typeof proof.action_sha256==='string' && /^[0-9a-f]{64}$/.test(proof.action_sha256) && proof.intent===expectedIntent &&
    ['destructive','network','privilege','recovery'].every(key=>proof[key]==='unknown') && proof.grant==='depends_on_choice';
  const material=canonicalMaterial(payload,sessionId);
  if (!validProof() || !material || !globalThis.crypto?.subtle) return null;
  try {
    const bytes=await globalThis.crypto.subtle.digest('SHA-256',new TextEncoder().encode(material));
    const digest=Array.from(new Uint8Array(bytes), byte=>byte.toString(16).padStart(2,'0')).join('');
    if (!validProof() || canonicalMaterial(payload,sessionId)!==material || digest!==proof.action_sha256) return null;
    return {intent:proof.intent};
  } catch (_) { return null; }
}

export function hasAdditionalOperationDetails(detail) {
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return false;
  const displayed=new Set(['command','argv','filepath','path','target','resource','workdir','cwd']);
  return Object.keys(detail).some(key=>!displayed.has(key));
}
