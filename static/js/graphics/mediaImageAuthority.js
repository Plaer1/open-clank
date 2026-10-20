// Presentation metadata is bound to an already-authorized loader result.
// This is not resource authority and never fetches a URL.
const images = new WeakMap();
export function markLoadedStillImage(image, {source, still, key}) {
  images.delete(image);
  if (still === true && typeof source === 'string' && source && typeof key === 'string' && key) {
    images.set(image,{source,key});
    if(typeof image.dispatchEvent==='function')image.dispatchEvent(new Event('openclank:still-image-ready',{bubbles:true}));
  }
}
export function forgetLoadedImage(image) { images.delete(image); }
export function loadedStillImage(image) {
  const item=images.get(image);
  return item && image.src === item.source ? item : null;
}
export function markTrustedJpegRepresentation(image,{mediaType,source,key,origin=globalThis.location?.origin}={}) {
  // Only already-authorized same-origin representations with explicit MIME;
  // no extension inference, external upload, proxy, or second acquisition.
  let safe=false;try{safe=!!origin&&new URL(source,origin).origin===origin;}catch(_){}
  markLoadedStillImage(image,{source,still:safe&&mediaType==='image/jpeg',key});
}
export async function classifyStillBlob(blob) {
  if (!blob || !Number.isSafeInteger(blob.size) || blob.size<3 || blob.size>4*1024*1024) return false;
  // At most 33 bytes of classification scratch per read; never copy the blob.
  const prefix=new Uint8Array(await blob.slice(0,Math.min(33,blob.size)).arrayBuffer());
  if (blob.type==='image/jpeg') return prefix[0]===255&&prefix[1]===216&&prefix[2]===255;
  if (blob.type!=='image/png'||prefix.length<33||![137,80,78,71,13,10,26,10].every((v,i)=>prefix[i]===v)) return false;
  let offset=8, chunks=0, header=false;
  while(offset+12<=blob.size&&chunks++<4096){
    const bytes=new Uint8Array(await blob.slice(offset,offset+8).arrayBuffer());
    const size=new DataView(bytes.buffer,bytes.byteOffset,8).getUint32(0);
    const type=String.fromCharCode(...bytes.subarray(4));
    if(size>blob.size-offset-12||type==='acTL')return false;
    if(!header){if(type!=='IHDR'||size!==13)return false;header=true;}
    offset+=size+12;
    if(type==='IEND')return size===0&&offset===blob.size;
  }
  return false;
}
