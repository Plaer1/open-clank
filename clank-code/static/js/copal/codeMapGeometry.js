const MAX_POINTS = 768;
const MAX_PINS = 64;
const MAX_FRAMES = 6;
const MAX_STORAGE = 600 * 1024;
const CANONICAL = /^(?:file|decl):[0-9a-f]{64}$/i;
const finitePoint = point => point && [point.x,point.y].every(value => Number.isFinite(value) && Math.abs(value)<=1e7);
const currentScope = receipt => receipt && JSON.stringify([receipt.workspace_id,receipt.registration_id,receipt.checkout_id,receipt.policy_generation]);

// Geometry is a local presentation cache. The current map must admit every
// resource again before any saved coordinate is applied.
export function createCodeMapGeometry(account, workspace, storage) {
  const key = account && workspace ? 'openclank-code-geometry:'+JSON.stringify([account,workspace]) : null;
  function load(receipt) {
    const scope = currentScope(receipt);
    if (!key || !scope || receipt.workspace_id!==workspace) return {scope:null,frames:[]};
    try {
      const text=storage.getItem(key);
      if (text && text.length<=MAX_STORAGE) {
        const value=JSON.parse(text);
        if (value?.version===1 && value.scope===scope && Array.isArray(value.frames)) return {scope,focus:CANONICAL.test(value.focus)?value.focus:null,frames:value.frames.slice(0,MAX_FRAMES)};
      }
    } catch { /* Optional browser-local storage. */ }
    return {scope,frames:[]};
  }
  function points(value) {
    const result = new Map();
    for (const point of Array.isArray(value) ? value.slice(0,MAX_POINTS) : []) {
      if (CANONICAL.test(point?.id) && finitePoint(point)) result.set(point.id,{id:point.id,x:point.x,y:point.y,pinned:point.pinned===true});
    }
    return result;
  }
  function read(receipt, layout) {
    const entry=load(receipt).frames.find(frame=>frame?.layout===layout);
    const stored=points(entry?.points);
    // Old gravity frames may contain row-packed overflow coordinates.
    // Keep explicit pins when re-seeding them with the radial layout.
    if(layout.startsWith('["gravity",') && entry?.radial!==true){
      for(const [id,point] of stored)if(!point.pinned)stored.delete(id);
    }
    return stored;
  }
  function write(receipt, layout, coordinates) {
    const {scope,frames,focus}=load(receipt);
    if (!scope || typeof layout!=='string' || layout.length>120) return false;
    const list=[...coordinates.values()], pins=list.filter(point=>point.pinned).slice(0,MAX_PINS);
    const remaining=list.filter(point=>!point.pinned).slice(0,MAX_POINTS-pins.length);
    const snapshot={version:1,scope,focus,frames:[{layout,...(layout.startsWith('["gravity",')?{radial:true}:{}),points:[...pins,...remaining]},...frames.filter(frame=>frame?.layout!==layout)].slice(0,MAX_FRAMES)};
    try {
      const text=JSON.stringify(snapshot);
      if (text.length>MAX_STORAGE) return false;
      storage.setItem(key,text);
      return true;
    } catch { return false; }
  }
  function remember(receipt, layout, next) {
    const prior=read(receipt,layout), merged=points(next);
    for (const [id,point] of prior) {
      if (point.pinned) merged.set(id,point);
      else if (!merged.has(id)) merged.set(id,point);
    }
    return write(receipt,layout,merged);
  }
  function togglePin(receipt, layout, point) {
    if (!CANONICAL.test(point?.id) || !finitePoint(point)) return false;
    const existing=read(receipt,layout), pinned=existing.get(point.id)?.pinned===true;
    if (!pinned && [...existing.values()].filter(row=>row.pinned).length>=MAX_PINS) return false;
    existing.set(point.id,{id:point.id,x:point.x,y:point.y,pinned:!pinned});
    return write(receipt,layout,existing);
  }
  function rememberFocus(receipt, focus) {
    const {scope,frames}=load(receipt);
    if (!scope || focus!==null && !CANONICAL.test(focus)) return false;
    try {
      const text=JSON.stringify({version:1,scope,frames,focus});
      if (text.length>MAX_STORAGE) return false;
      storage.setItem(key,text);
      return true;
    } catch { return false; }
  }
  return {read,remember,togglePin,rememberFocus,focus:receipt=>load(receipt).focus||null};
}
