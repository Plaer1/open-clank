// ArtProject metadata is descriptive JSON, never a resource grant.
const encoder = new TextEncoder();
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const HEX = /^[a-f0-9]{64}$/;

function plain(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
}
function jsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'boolean') return true;
  if (typeof value === 'string') return value.isWellFormed();
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value !== 'object' || seen.has(value) || (!Array.isArray(value) && !plain(value))) return false;
  seen.add(value);
  const keys = Reflect.ownKeys(value);
  if (keys.some(k => typeof k !== 'string' || !k.isWellFormed())) return false;
  if (Array.isArray(value) && (keys.length !== value.length + 1 || keys[value.length] !== 'length')) return false;
  for (const key of keys) {
    if (Array.isArray(value) && key === 'length') continue;
    const d = Object.getOwnPropertyDescriptor(value, key);
    if (!d || !d.enumerable || !Object.hasOwn(d, 'value') || !jsonValue(d.value, seen)) return false;
  }
  seen.delete(value);
  return true;
}
function closed(value, required, optional = []) {
  if (!plain(value)) return false;
  const keys = Object.keys(value);
  return required.every(k => Object.hasOwn(value, k)) && keys.every(k => required.includes(k) || optional.includes(k));
}
function string(value, max, {empty = false, style = false} = {}) {
  if (typeof value !== 'string' || !value.isWellFormed() || (!empty && !value.length) || encoder.encode(value).length > max) return false;
  for (const ch of value) {
    const n = ch.codePointAt(0);
    if ((n < 32 || (n >= 127 && n <= 159)) && !(style && (n === 9 || n === 10))) return false;
  }
  return true;
}
const id = value => typeof value === 'string' && ID.test(value);
const integer = value => Number.isSafeInteger(value);

export function validateArtProject(value) {
  try {
    if (!jsonValue(value) || !closed(value, ['version','references','palette','style','clips']) || value.version !== 1 || encoder.encode(JSON.stringify(value)).length > 65536) return false;
    if (!Array.isArray(value.references) || value.references.length > 64 || !Array.isArray(value.palette) || value.palette.length > 64 || !Array.isArray(value.clips) || value.clips.length > 32 || !string(value.style, 2048, {empty:true,style:true})) return false;
    const refs = new Set(), colors = new Set(), clips = new Set(), frames = new Set();
    for (const r of value.references) {
      if (!closed(r, ['id','label','resourceKey','revision'], ['sha256']) || !id(r.id) || refs.has(r.id) || !string(r.label,128)) return false;
      refs.add(r.id);
      const k=r.resourceKey, rev=r.revision;
      if (!closed(k,['provider','account_id','workspace_id','resource_id']) || !string(k.provider,128) || !string(k.account_id,256) || !(k.workspace_id === null || string(k.workspace_id,256)) || !string(k.resource_id,2048) || !closed(rev,['kind','value']) || !string(rev.kind,128) || !string(rev.value,2048) || (Object.hasOwn(r,'sha256') && (typeof r.sha256 !== 'string' || !HEX.test(r.sha256)))) return false;
    }
    for (const p of value.palette) {
      if (!closed(p,['id','name','color']) || !id(p.id) || colors.has(p.id) || !string(p.name,128) || typeof p.color !== 'string' || !/^#[0-9a-fA-F]{8}$/.test(p.color)) return false;
      colors.add(p.id);
    }
    for (const c of value.clips) {
      if (!closed(c,['id','name','facing','frames']) || !id(c.id) || clips.has(c.id) || !string(c.name,128) || !string(c.facing,128,{empty:true}) || !Array.isArray(c.frames) || !c.frames.length) return false;
      clips.add(c.id);
      for (const f of c.frames) {
        if (!closed(f,['id','referenceId','width','height','pivot']) || !id(f.id) || frames.has(f.id) || frames.size >= 256 || !(f.referenceId === null || (id(f.referenceId) && refs.has(f.referenceId))) || !integer(f.width) || f.width < 1 || f.width > 16384 || !integer(f.height) || f.height < 1 || f.height > 16384) return false;
        frames.add(f.id);
        if (f.pivot !== null && (!closed(f.pivot,['x','y']) || !integer(f.pivot.x) || f.pivot.x < 0 || f.pivot.x > f.width || !integer(f.pivot.y) || f.pivot.y < 0 || f.pivot.y > f.height)) return false;
      }
    }
    return true;
  } catch (_) { return false; }
}

export function cloneArtProject(value) {
  if (!jsonValue(value)) throw new TypeError('ArtProject value must be JSON');
  return JSON.parse(JSON.stringify(value));
}
export function emptyArtProject() { return {version:1,references:[],palette:[],style:'',clips:[]}; }
export function inspectArtProject(value, present = true) {
  if (!present) return {state:'absent'};
  try {
    if (!jsonValue(value) || !plain(value) || !Number.isSafeInteger(value.version) || value.version < 1) return {state:'invalid'};
    if (value.version !== 1) return {state:'unsupported',value:cloneArtProject(value)};
    return validateArtProject(value) ? {state:'supported',value:cloneArtProject(value)} : {state:'invalid'};
  } catch (_) { return {state:'invalid'}; }
}
export function artProjectEqual(a,b) {
  if (!jsonValue(a) || !jsonValue(b)) return false;
  const equal=(x,y)=>{
    if (x === y) return true;
    if (!x || !y || typeof x !== 'object' || typeof y !== 'object' || Array.isArray(x) !== Array.isArray(y)) return false;
    const xk=Object.keys(x), yk=Object.keys(y);
    return xk.length===yk.length && xk.every(k=>Object.hasOwn(y,k) && equal(x[k],y[k]));
  };
  return equal(a,b);
}
