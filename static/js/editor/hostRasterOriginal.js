// Host images keep layered recovery in the editor draft and flatten only the
// original raster. Browser encoders cannot preserve animation.
export function hostRasterMime(bytes, type) {
  const view = new DataView(bytes), size=bytes.byteLength;
  type=String(type || '').split(';')[0].trim().toLowerCase();
  if (type==='image/jpeg' && size>=4 && view.getUint16(0)===0xffd8 && view.getUint8(2)===0xff) return type;
  if (type==='image/png' && size>=33 && view.getUint32(0)===0x89504e47 && view.getUint32(4)===0x0d0a1a0a &&
      view.getUint32(8)===13 && view.getUint32(12)===0x49484452) {
    for (let offset=8; offset+12<=size;) {
      const length=view.getUint32(offset), tag=view.getUint32(offset+4);
      if (offset+12+length>size || tag===0x6163544c) return null; // acTL
      if (tag===0x49444154) return type; // IDAT
      offset+=12+length;
    }
  }
  if (type==='image/webp' && size>=20 && view.getUint32(0)===0x52494646 && view.getUint32(8)===0x57454250 &&
      view.getUint32(4,true)+8===size) {
    let raster=false;
    for (let offset=12; offset+8<=size;) {
      const length=view.getUint32(offset+4,true), tag=view.getUint32(offset);
      if (offset+8+length>size || tag===0x414e494d || tag===0x414e4d46) return null; // ANIM / ANMF
      if (tag===0x56503858 && (length!==10 || view.getUint8(offset+8)&2)) return null; // VP8X animation
      if (tag===0x56503820 || tag===0x5650384c) raster=true; // VP8 / VP8L
      offset+=8+length+(length%2);
    }
    if (raster) return type;
  }
  return null;
}

const hostId = value => typeof value==='string' && /^resource-[a-f0-9]{64}$/.test(value);
const fingerprint = value => typeof value==='string' && /^sha256:[a-f0-9]{64}:[1-9][0-9]{0,15}$/.test(value);
const safeName = name => typeof name==='string' && name.length>0 && name.length<=1024 && !/[\x00-\x1f\x7f]/.test(name);

export function hostRasterRecovery(original,target) {
  if (!original) return null;
  const {resourceId,resourceRef,revision,mime,name,trail,pendingFingerprint}=original;
  if (!hostId(resourceId) || typeof resourceRef!=='string' || !resourceRef || resourceRef.length>8192 ||
      revision?.kind!=='hostFingerprint' || !fingerprint(revision.value) || ![null,'image/png','image/jpeg','image/webp'].includes(mime) ||
      !safeName(name) || (pendingFingerprint!=null && !fingerprint(pendingFingerprint))) throw Error('Invalid saved image destination.');
  const folders=Array.isArray(trail) && trail.length<=64 && trail.every(row=>hostId(row?.id) && safeName(row?.name))
    ? trail.map(({id,name})=>({id,name})) : [];
  return {v:1,sessionId:target.sessionId,scope:{...target.scope},resourceId,resourceRef,revision:{kind:'hostFingerprint',value:revision.value},mime,name,trail:folders,pendingFingerprint:pendingFingerprint || null};
}

export async function hostRasterTrail(client,row) {
  try {
    const revealed=await client.reveal(row.ref);
    if (revealed.resource?.id===row.id && revealed.parent?.id===revealed.ancestors?.at(-1)?.id) {
      return revealed.ancestors.map(({id,name})=>({id,name}));
    }
  } catch { /* Live refs remain usable for sources without a browsable parent. */ }
  return [];
}

export async function resolveHostRaster(client,original,isCurrent) {
  let row;
  try { const response=await client.stat(original.resourceRef); row=response?.resource || response; }
  catch (error) {
    if (!isCurrent() || !original.trail?.length) throw error;
    const {resolveEditorHostFolder}=await import('../copal/editorFilesBrowser.js');
    const parent=original.trail.at(-1);
    const folder=await resolveEditorHostFolder(client,{resourceId:parent.id,name:parent.name},original.trail);
    if (!folder?.ref) throw Error('The original image folder is unavailable. Use Save as.');
    let cursor=null; const visited=new Set();
    do {
      if (!isCurrent()) throw Error('Image destination changed.');
      const page=await client.children(folder.ref,{cursor,query:original.name,limit:100});
      row=page.entries?.find(entry=>entry.id===original.resourceId);
      cursor=page.next_cursor;
      if (!cursor || visited.has(cursor)) break;
      visited.add(cursor);
    } while (!row);
    if (!row) throw Error('The original image is unavailable. Use Save as.');
    const response=await client.stat(row.ref); row=response?.resource || response;
  }
  if (!isCurrent() || row?.provider!=='host' || row.kind!=='file' || row.id!==original.resourceId || !row.ref ||
      row.revision?.kind!=='hostFingerprint' || !fingerprint(row.revision.value) || !row.capabilities?.includes('write') ||
      (original.mime && row.mime_type!==original.mime)) throw Error('The original image is unavailable for writing. Use Save as.');
  original.resourceRef=row.ref;
  if (original.pendingFingerprint===row.revision.value) { original.revision={...row.revision}; original.pendingFingerprint=null; }
  if (row.revision.value!==original.revision.value) throw Error('The original changed in Files. Your edits remain open. Use Save as.');
  return row;
}

export async function hostRasterFingerprint(blob) {
  const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()));
  return 'sha256:'+Array.from(digest,byte=>byte.toString(16).padStart(2,'0')).join('')+':'+blob.size;
}

export async function writeHostRaster(original, blob, isCurrent, fetchImpl = fetch, nextFingerprint = null) {
  if (!original.mime || blob.type !== original.mime) throw new Error('Save as copy to preserve the source image format.');
  if (blob.size > 32 * 1024 * 1024) throw new Error('This encoded image exceeds the 32 MiB editor limit.');
  nextFingerprint ??= await hostRasterFingerprint(blob);
  const expected = original.revision.value;
  const query = new URLSearchParams({resource_ref:original.resourceRef, expected_fingerprint:expected});
  if (!isCurrent()) throw new Error('The original image session changed before saving.');
  let response, appliedReceipt;
  try {
    response = await fetchImpl('/api/files-v1/save-raster?' + query, {
      method:'POST', credentials:'same-origin', headers:{'Content-Type':blob.type}, body:blob,
    });
    if (response.ok) {
      const receipt = await response.json();
      if (receipt.outcome === 'applied' && receipt.revision?.kind === 'hostFingerprint' && receipt.revision.value === nextFingerprint && isCurrent()) {
        appliedReceipt = receipt;
      }
    }
  } catch { /* An interrupted response can follow a committed replacement. */ }
  if (!isCurrent()) throw new Error('The image session changed. Reopen the original to check its save.');
  const result = await original.stat(original.resourceRef);
  const row = result?.resource || result;
  if (isCurrent() && row?.id === original.resourceId && row.provider==='host' && row.kind==='file' && row.revision?.kind === 'hostFingerprint' && row.revision.value === nextFingerprint) {
    original.revision = row.revision;
    return appliedReceipt || {outcome:'applied',revision:row.revision};
  }
  if (response?.status === 413) throw new Error('This encoded image exceeds the Files save limit. Your edits remain open.');
  if (response?.status === 409 || (row?.id === original.resourceId && row?.revision?.value !== expected)) throw new Error('The original changed in Files. Your edits remain open. Use Save as copy.');
  throw new Error('The original save could not be verified. Your edits remain open.');
}
