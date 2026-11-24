// App-owned Graph/Files/Editor admission. Not browser-wide GPU/RSS accounting.
export const GRAPHICAL_SURFACE_LIMITS = Object.freeze({contexts:4, mediaBytes:32*1024*1024, regionBytes:16*1024*1024, images:64, imagePixels:1024*1024, canvasPixels:4*1024*1024, dpr:2});
export function createSurfaceBudget({contexts=GRAPHICAL_SURFACE_LIMITS.contexts, mediaBytes=GRAPHICAL_SURFACE_LIMITS.mediaBytes} = {}) {
  if (!Number.isSafeInteger(contexts) || contexts<1 || !Number.isSafeInteger(mediaBytes) || mediaBytes<0) throw new TypeError('Invalid graphics budget');
  const leases = new Map();
  let bytes=0;
  return {
    acquire(id) {
      if (typeof id!=='string'||!id||id.length>256||leases.has(id)||leases.size>=contexts) return null;
      const lease={bytes:0,released:false}; leases.set(id,lease);
      return {
        reserve(next) {
          if (lease.released||!Number.isSafeInteger(next)||next<0||next>GRAPHICAL_SURFACE_LIMITS.regionBytes||bytes-lease.bytes+next>mediaBytes) return false;
          bytes+=next-lease.bytes;lease.bytes=next;return true;
        },
        release() { if (lease.released) return; lease.released=true;bytes-=lease.bytes;leases.delete(id); },
      };
    },
    snapshot() { return {contexts:leases.size,mediaBytes:bytes,maxContexts:contexts,maxMediaBytes:mediaBytes}; },
  };
}
let browserBudget;
export function graphicalSurfaceBudget() { return browserBudget ||= createSurfaceBudget(); }
