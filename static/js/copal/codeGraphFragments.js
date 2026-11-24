// Independent bounded user-supplied observations, never source authority.
import { MAP_RELATIONS } from './codeMapModel.js';
const SOURCE_RELATIONS = new Set(MAP_RELATIONS.filter(kind => kind !== 'containment'));
const MAX_BYTES=1024*1024, MAX_ITEMS=1000;
const operations=new Set(['overview','catalog','outline','inspect','neighbors','path','impact']);
const id=/^(file|decl|occ):[0-9a-f]{64}$/;
const hash=/^[0-9a-f]{64}$/;
const token=/^[a-zA-Z0-9._:-]{1,256}$/;
const fields=new Set(['label','source','signature','documentation','occurrences','resolution','properties','excerpt','witnesses']);
const channels=new Set(['exact','identifier','fuzzy','lexical','structural','semantic']);
const reasons=new Set(['authorized_files_owner_required','configured_semantic_producer_not_admitted','import_resolution_incomplete','source_coverage_partial','projection_budget_exceeded','unknown']);
const safe=v=>typeof v==='string' && token.test(v);
const obj=v=>v && typeof v==='object' && !Array.isArray(v);
const keys=(v,expected)=>obj(v)&&Object.keys(v).sort().join(',')===[...expected].sort().join(',');
const copy=v=>JSON.parse(JSON.stringify(v));
const ordered=v=>JSON.stringify(v,Object.keys(v).sort());
function scopeOf(query){
  if(!obj(query)||!operations.has(query.operation)||(query.query!=null&&query.query!=='')||query.read!=null||query.cursor!=null||query.channels?.includes('raw'))throw Error('Unsupported evidence scope');
  const allowed=['schema','operation','query','fields','filter','budgets','seed_ids','target_id','direction','channels','expected_receipt','request_id'];
  if(Object.keys(query).some(k=>!allowed.includes(k))||query.schema!=='open-clank.code-query/v1')throw Error('Unsupported evidence scope');
  const clean=copy(query);delete clean.query;delete clean.expected_receipt;delete clean.request_id;delete clean.cursor;
  const array=(v,max=64)=>Array.isArray(v)&&v.length<=max&&v.every(s=>safe(s))&&new Set(v).size===v.length;
  if(!array(clean.fields)||clean.fields.some(x=>!fields.has(x))||clean.channels!=null&&(!array(clean.channels)||clean.channels.some(x=>!channels.has(x)))||clean.seed_ids!=null&&(!Array.isArray(clean.seed_ids)||clean.seed_ids.length>64||clean.seed_ids.some(x=>!id.test(x)))||clean.target_id!=null&&!id.test(clean.target_id)||clean.direction!=null&&!['incoming','outgoing','both'].includes(clean.direction))throw Error('Invalid scope');
  if(clean.filter!=null && (!obj(clean.filter)||Object.keys(clean.filter).some(k=>!['rel_path','path_prefix','kinds','symbol_kinds','languages','states'].includes(k))||['kinds','symbol_kinds','languages','states'].some(k=>clean.filter[k]!=null&&!array(clean.filter[k]))))throw Error('Invalid filter');
  if(!obj(clean.budgets)||Object.keys(clean.budgets).some(k=>!['rows','bytes','tokens','hops','work'].includes(k))||Object.values(clean.budgets).some(n=>!Number.isSafeInteger(n)||n<0||n>10000000))throw Error('Invalid budget');
  for(const k of ['rel_path','path_prefix']){const p=clean.filter?.[k];if(p!==undefined&&(typeof p!=='string'||p.length>1024||p.startsWith('/')||p.includes('\\')||p.includes(':')||p.split('/').some(s=>s==='..'||s==='.')||/[\u0000-\u001f\u007f]/.test(p)))throw Error('Invalid scope');}
  if(JSON.stringify(clean).length>16384)throw Error('Scope too large');
  return clean;
}
function validate(value){
  if(!keys(value,['schema','identity','scope','receipt','coverage','nodes','edges'])||value.schema!=='openclank.code-fragment.v1'||value.identity!=='code-evidence.v1')throw Error('Invalid fragment');
  const scope=scopeOf(value.scope);if(JSON.stringify(scope)!==JSON.stringify(value.scope))throw Error('Invalid scope');
  const receipt=value.receipt, rkeys=['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'];
  if(!keys(receipt,rkeys)||rkeys.some(k=>['admission_epoch','policy_generation'].includes(k)?!Number.isSafeInteger(receipt[k])||receipt[k]<0:!safe(receipt[k])))throw Error('Invalid receipt');
  if(!keys(value.coverage,['partial','degraded','has_more','omissions','reasons'])||['partial','degraded','has_more'].some(k=>typeof value.coverage[k]!=='boolean')||!Array.isArray(value.coverage.omissions)||value.coverage.omissions.length>64||value.coverage.omissions.some(x=>!keys(x,['field','reason'])||!(fields.has(x.field)||x.field==='unknown')||!['not_requested','unknown'].includes(x.reason))||!Array.isArray(value.coverage.reasons)||value.coverage.reasons.length>64||value.coverage.reasons.some(x=>!reasons.has(x)))throw Error('Invalid coverage');
  if(!Array.isArray(value.nodes)||!Array.isArray(value.edges)||value.nodes.length>MAX_ITEMS||value.edges.length>MAX_ITEMS)throw Error('Fragment too large');
  const seen=new Set();for(const n of value.nodes){if(!keys(n,['id','kind','identity_state','file_id','source_hash'])||!id.test(n.id)||!({file:'file:',declaration:'decl:',occurrence:'occ:'}[n.kind]&&n.id.startsWith({file:'file:',declaration:'decl:',occurrence:'occ:'}[n.kind]))||!['stable','ambiguous_snapshot_local','unknown'].includes(n.identity_state)||(n.kind!=='declaration'&&n.identity_state!=='unknown')||(n.file_id!==null&&(!id.test(n.file_id)||!n.file_id.startsWith('file:')))||(n.kind==='file'&&n.file_id!==n.id)||(n.source_hash!==null&&!hash.test(n.source_hash))||seen.has(n.id))throw Error('Invalid node');seen.add(n.id);}
  const edges=new Set();for(const e of value.edges){if(!keys(e,['from','to','occurrence_id','relation_kind','resolution_state','file_id','source_hash'])||![e.from,e.to,e.occurrence_id].every(x=>id.test(x))||!e.from.startsWith('decl:')||!e.to.startsWith('decl:')||!e.occurrence_id.startsWith('occ:')||!SOURCE_RELATIONS.has(e.relation_kind)||!['exact','inferred'].includes(e.resolution_state)||(e.file_id!==null&&(!id.test(e.file_id)||!e.file_id.startsWith('file:')))||(e.source_hash!==null&&!hash.test(e.source_hash)))throw Error('Invalid edge');const key=JSON.stringify([e.from,e.to,e.occurrence_id]);if(edges.has(key))throw Error('Duplicate edge');edges.add(key);}
  return value;
}
export function captureCodeFragment(data,query){
  if(data?.status!=='ready'||data.error||!data.receipt||data.raw_windows?.length||data.raw_file_matches?.length||!Array.isArray(data.nodes)||!Array.isArray(data.edges)||typeof data.partial!=='boolean'||typeof data.degraded!=='boolean'||!Array.isArray(data.omissions)||!Array.isArray(data.reasons)||data.omissions.length>64||data.reasons.length>64||!(data.next_cursor===null||typeof data.next_cursor==='string'))throw Error('Current evidence unavailable');
  const nodes=data.nodes.map(n=>({id:n.id,kind:n.kind,identity_state:n.kind==='declaration'?(n.properties?.identity_state||'unknown'):'unknown',file_id:n.id?.startsWith('file:')?n.id:(n.source?.file_id?`file:${n.source.file_id}`:null),source_hash:n.properties?.source_hash||n.source?.source_hash||null}));
  const edges=data.edges.map(e=>({from:e.from,to:e.to,occurrence_id:e.occurrence_id,relation_kind:e.relation_kind,resolution_state:e.resolution_state,file_id:e.source?.file_id?`file:${e.source.file_id}`:null,source_hash:e.source?.source_hash||null}));
  const receipt=Object.fromEntries(['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'].map(k=>[k,data.receipt[k]]));
  const omissions=data.omissions.map(x=>({field:fields.has(x?.field)?x.field:'unknown',reason:x?.reason==='not_requested'?'not_requested':'unknown'}));
  const coverageReasons=data.reasons.map(x=>reasons.has(x)?x:'unknown');
  const value={schema:'openclank.code-fragment.v1',identity:'code-evidence.v1',scope:scopeOf(query),receipt,coverage:{partial:data.partial===true,degraded:data.degraded===true,has_more:!!data.next_cursor,omissions,reasons:coverageReasons},nodes,edges};
  return decodeCodeFragment(encodeCodeFragment(value));
}
export function encodeCodeFragment(value){validate(value);const text=JSON.stringify(value);if(new TextEncoder().encode(text).length>MAX_BYTES)throw Error('Fragment too large');return text;}
export function decodeCodeFragment(text){if(typeof text!=='string'||new TextEncoder().encode(text).length>MAX_BYTES)throw Error('Fragment too large');const parsed=JSON.parse(text);if(JSON.stringify(parsed)!==text)throw Error('Noncanonical fragment');return copy(validate(parsed));}
export function compareCodeFragments(recorded,current){
  validate(recorded);validate(current);const a=recorded.receipt,b=current.receipt;
  if(['workspace_id','registration_id','checkout_id','config_fingerprint'].some(k=>a[k]!==b[k])||JSON.stringify(recorded.scope)!==JSON.stringify(current.scope))throw Error('Incompatible evidence scope');
  const compare=(left,right,key)=>{const x=new Map(left.map(v=>[key(v),v])),y=new Map(right.map(v=>[key(v),v]));return {both:[...x.keys()].filter(k=>y.has(k)).length,recorded_only:[...x.keys()].filter(k=>!y.has(k)),current_only:[...y.keys()].filter(k=>!x.has(k)),changed:[...x.keys()].filter(k=>y.has(k)&&ordered(x.get(k))!==ordered(y.get(k))).map(k=>{const before=x.get(k),after=y.get(k);return {key:k,fields:Object.keys(before).filter(f=>before[f]!==after[f]).map(f=>({field:f,recorded:before[f],current:after[f]})),continuity:before.identity_state==='stable'&&after.identity_state==='stable'?'stable_key_only':'unknown'};})};};
  return {nodes:compare(recorded.nodes,current.nodes,n=>n.id),edges:compare(recorded.edges,current.edges,e=>JSON.stringify([e.from,e.to,e.occurrence_id])),recorded_coverage:copy(recorded.coverage),current_coverage:copy(current.coverage),recorded_head:a.head_generation,current_head:b.head_generation};
}
