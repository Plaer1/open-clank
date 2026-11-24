// Disposable C04 projection only. No indexing, persistence authority or ambient context.
const SCHEMA = 'open-clank.code-query/v1';
const CHANNELS = new Set(['exact','identifier','fuzzy','lexical','structural','raw','semantic']);
const RELATIONS = new Set(['call','import','value_reference','type_reference','inherits','implements','render','route','overrides','manifest_dependency','sql_table_reference','callback_registration','event_publish','event_consume']);
const RECEIPT = ['workspace_id', 'registration_id', 'source_snapshot_id', 'checkout_id', 'head_generation', 'admission_epoch', 'policy_generation', 'config_fingerprint'];
const canonicalId = id => typeof id === 'string' && /^(file|decl|occ):[0-9a-f]{1,128}$/i.test(id);
const record = value => value && typeof value === 'object' && !Array.isArray(value);
function validReceipt(receipt) {
  return record(receipt) && RECEIPT.every(key => ['admission_epoch','policy_generation'].includes(key)
    ? Number.isSafeInteger(receipt[key]) && receipt[key] >= 0 : typeof receipt[key] === 'string' && receipt[key].length > 0);
}
function validEdge(edge) {
  return record(edge) && canonicalId(edge.from) && canonicalId(edge.to) && typeof edge.occurrence_id === 'string'
    && /^occ:[0-9a-f]{1,128}$/i.test(edge.occurrence_id) && typeof edge.resolution_state === 'string' && RELATIONS.has(edge.relation_kind)
    && (!('witnesses' in edge) || (Array.isArray(edge.witnesses) && edge.witnesses.every(record)))
    && (!('source' in edge) || record(edge.source));
}
const ANALYSIS_COUNTS = ['file_count','edge_count','inferred','ambiguous','unresolved','unsupported','excluded','missing'];
function validImportAnalysis(value) {
  if (!record(value) || Object.keys(value).some(key => !['policy','units',...ANALYSIS_COUNTS].includes(key)) || value.policy !== 'exact_imports_v1' || !Array.isArray(value.units)
      || !ANALYSIS_COUNTS.every(key => Number.isSafeInteger(value[key]) && value[key] >= 0)) return false;
  return value.units.every(unit => record(unit) && (unit.kind === 'cycle'
    ? Object.keys(unit).every(k => ['kind','node_ids','occurrence_ids'].includes(k))
      && Array.isArray(unit.node_ids) && unit.node_ids.length > 0 && unit.node_ids.length <= 200
      && unit.node_ids.every(id => canonicalId(id) && id.startsWith('file:'))
      && Array.isArray(unit.occurrence_ids) && unit.occurrence_ids.length > 0 && unit.occurrence_ids.length <= 200
      && unit.occurrence_ids.every(id => canonicalId(id) && id.startsWith('occ:'))
    : unit.kind === 'hub' && Object.keys(unit).every(k => ['kind','node_id','in_degree','out_degree'].includes(k))
      && canonicalId(unit.node_id) && unit.node_id.startsWith('file:')
      && ['in_degree','out_degree'].every(k => Number.isSafeInteger(unit[k]) && unit[k] >= 0)));
}
const digest64 = value => typeof value==='string' && /^[0-9a-f]{64}$/.test(value);
const spanKeys=['start_byte','end_byte','start_line','start_column','end_line','end_column'];
function validSpan(value, parent) {
  return record(value) && Object.keys(value).length===spanKeys.length && spanKeys.every(k=>Number.isSafeInteger(value[k])&&value[k]>=0)
    && value.start_byte<=value.end_byte && value.start_line>=1 && value.end_line>=value.start_line
    && (!parent || parent.start_byte<=value.start_byte && value.end_byte<=parent.end_byte);
}
function validSlice(value, seed) {
  const keys=['version','profile','function_id','seed_occurrence_id','source','return_span','root_definition_id','units'];
  if(!record(value)||Object.keys(value).length!==keys.length||!keys.every(k=>k in value)||value.version!==1||value.profile!=='python_straight_line_v1'||!digest64(value.function_id)||!digest64(value.seed_occurrence_id)||seed!==`occ:${value.seed_occurrence_id}`||!digest64(value.root_definition_id)||!record(value.source)||Object.keys(value.source).sort().join(',')!=='file_id,rel_path,source_hash,span'||!digest64(value.source.file_id)||!digest64(value.source.source_hash)||typeof value.source.rel_path!=='string'||!value.source.rel_path||value.source.rel_path.startsWith('/')||value.source.rel_path.includes('\\')||value.source.rel_path.includes('\0')||value.source.rel_path.split('/').some(p=>!p||p==='.'||p==='..')||new TextEncoder().encode(value.source.rel_path).length>4096||!validSpan(value.source.span)||!validSpan(value.return_span,value.source.span)||!Array.isArray(value.units)||value.units.length>64)return false;
  let uses=0;const ids=new Set(),occurrences=new Set();
  for(const unit of value.units){
    if(!record(unit)||!digest64(unit.id)||ids.has(unit.id)||!['parameter','assignment'].includes(unit.kind)||typeof unit.name!=='string'||!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(unit.name)||!validSpan(unit.target_span,value.source.span)||!Array.isArray(unit.uses)||unit.uses.length>16)return false;
    const keys=unit.kind==='parameter'?['id','kind','name','target_span','uses']:['id','kind','name','target_span','statement_span','uses'];
    if(Object.keys(unit).length!==keys.length||!keys.every(k=>k in unit))return false;
    if(unit.kind==='parameter'&&unit.uses.length)return false;
    if(unit.kind==='assignment'&&(!validSpan(unit.statement_span,value.source.span)||!validSpan(unit.target_span,unit.statement_span)))return false;
    ids.add(unit.id);
    for(const use of unit.uses){if(!record(use)||Object.keys(use).sort().join(',')!=='definition_id,occurrence_id,span'||!digest64(use.occurrence_id)||occurrences.has(use.occurrence_id)||!digest64(use.definition_id)||!validSpan(use.span,unit.statement_span)||use.span.start_byte<unit.target_span.end_byte)return false;occurrences.add(use.occurrence_id);uses++;}
  }
  return uses<=256;
}
function validBoundaries(value, seed) {
  const keys=['version','profile','function_id','source','regions'];
  if(!record(value)||Object.keys(value).length!==keys.length||!keys.every(k=>k in value)||value.version!==1||value.profile!=='python_try_except_v1'||!digest64(value.function_id)||seed!==`decl:${value.function_id}`||!record(value.source)||Object.keys(value.source).sort().join(',')!=='file_id,rel_path,source_hash,span'||!digest64(value.source.file_id)||!digest64(value.source.source_hash)||typeof value.source.rel_path!=='string'||!value.source.rel_path||value.source.rel_path.startsWith('/')||value.source.rel_path.includes('\\')||value.source.rel_path.includes('\0')||value.source.rel_path.split('/').some(p=>!p||p==='.'||p==='..')||!validSpan(value.source.span)||!Array.isArray(value.regions)||value.regions.length>64)return false;
  let handlers=0,calls=0;const starts=new Set();
  for(const unit of value.regions){
    if(!record(unit)||!['region','body','handlers','calls'].every(k=>k in unit)||Object.keys(unit).some(k=>!['region','body','handlers','calls','else_clause','finally_clause'].includes(k))||!validSpan(unit.region,value.source.span)||!validSpan(unit.body,unit.region)||unit.body.start_byte===unit.body.end_byte||starts.has(unit.region.start_byte)||!Array.isArray(unit.handlers)||!unit.handlers.length||!Array.isArray(unit.calls))return false;
    starts.add(unit.region.start_byte);let previous=unit.body.end_byte;
    for(const part of [...unit.handlers,...(unit.else_clause?[unit.else_clause]:[]),...(unit.finally_clause?[unit.finally_clause]:[])]){if(!validSpan(part,unit.region)||part.start_byte>=part.end_byte||part.start_byte<previous)return false;previous=part.end_byte;}
    const ids=new Set();for(const call of unit.calls){if(!record(call)||Object.keys(call).sort().join(',')!=='id,span'||!digest64(call.id)||ids.has(call.id)||!validSpan(call.span,unit.body)||call.span.start_byte>=call.span.end_byte)return false;ids.add(call.id);}
    handlers+=unit.handlers.length;calls+=unit.calls.length;
  }
  return handlers<=128&&calls<=256;
}
function boundaryIdentity(value){const {regions,...identity}=value;return JSON.stringify(identity);}
function sliceIdentity(value){const {units,...identity}=value;return JSON.stringify(identity);}
const clone = value => structuredClone(value);
const FACET_AXES = ['kinds','languages','states','folders'];
export const codeFacetCount = facets => FACET_AXES.reduce((total,axis)=>total+(facets?.[axis]?.length||0),0);
function validFacets(facets) {
  return record(facets) && Object.keys(facets).every(axis=>FACET_AXES.includes(axis))
    && Object.values(facets).every(rows=>Array.isArray(rows) && rows.every(row=>record(row)
      && typeof row.value==='string' && typeof row.unit==='string' && row.unit.length>0
      && ['exact','unknown'].includes(row.exactness)
      && (Number.isSafeInteger(row.count) && row.count>=0 || row.count===null && row.exactness==='unknown')));
}
function mergeFacets(prior,next) {
  return Object.fromEntries(FACET_AXES.map(axis=>{
    const rows=new Map((prior?.[axis]||[]).map(row=>[row.value,row]));
    for(const row of next[axis]||[])rows.set(row.value,clone(row));
    return [axis,[...rows.values()]];
  }));
}
export const codeReceiptKey = receipt => receipt && JSON.stringify(RECEIPT.map(key => [key, receipt[key] ?? null]));
export const codeEdgeId = edge => JSON.stringify([edge.from, edge.to, edge.occurrence_id]);
export function codeQueryKey(query) {
  const { cursor, expected_receipt, request_id, ...selection } = query;
  return JSON.stringify(selection);
}
export function createCodeGraphModel({ request, isCurrent, changed = () => {}, limits = {} }) {
  const ceilings={nodes:1000,edges:1000,paths:100,facets:1000},bounds=Object.fromEntries(Object.entries(ceilings).map(([key,max])=>[key,Number.isSafeInteger(limits[key])&&limits[key]>0?Math.min(max,limits[key]):max]));
  let serial = 0, controller = null, disposed = false;
  let data = { status:'idle', receipt:null, query:null, nodes:[], raw_file_matches:[], raw_windows:[], edges:[], paths:[], counts:[], facets:{}, capabilities:[], operation_capabilities:[], omissions:[], channels:[], partial:false, degraded:false, reasons:[], next_cursor:null, error:null, returned:0 };
  const snapshot = () => clone(data);
  function dispose() { disposed = true; serial += 1; controller?.abort(); }
  async function runInternal(workspaceId, query, { more = false, qualifiedResult } = {}) {
    if (disposed || !isCurrent()) return false;
    if (query.channels && (!Array.isArray(query.channels) || query.channels.some(channel=>!CHANNELS.has(channel)))) return false;
    const kinds=query.filter?.symbol_kinds;
    if(kinds!==undefined&&(!Array.isArray(kinds)||kinds.length>64||kinds.some(kind=>typeof kind!=='string'||new TextEncoder().encode(kind).length>128)))return false;
    if ((query.seed_ids || []).some(id => !canonicalId(id)) || (query.target_id && !canonicalId(query.target_id))) return false;
    const selection = codeQueryKey(query);
    if(more&&(data.nodes.length>=bounds.nodes||data.edges.length>=bounds.edges||data.paths.length>=bounds.paths||codeFacetCount(data.facets)>=bounds.facets))return false;
    if (more && (!data.next_cursor || !data.receipt || selection !== data.query)) return false;
    const prior = more ? snapshot() : { ...snapshot(), nodes:[], raw_file_matches:[], raw_windows:[], edges:[], paths:[], counts:[], facets:{}, receipt:null, next_cursor:null, returned:0, omissions:[], channels:[], slice_analysis:null,error_boundary_analysis:null,pipeline_stages:[] };
    controller?.abort(); controller = new AbortController();
    const ticket = ++serial, signal = controller.signal;
    const current = () => !disposed && ticket === serial && isCurrent();
    data = { ...prior, operation:query.operation, facet_returned:0, status:'loading', error:null }; changed(snapshot());
    const payload = clone(query);
    if (more) { payload.cursor = prior.next_cursor; payload.expected_receipt = clone(prior.receipt); }
    else if (query.expected_receipt) payload.expected_receipt = clone(query.expected_receipt);
    try {
      const result = qualifiedResult === undefined ? await request(workspaceId, payload, signal) : clone(qualifiedResult);
      if (!current()) return false;
      const invalid = () => { throw Object.assign(new Error('Invalid code projection'),{code:'invalid_evidence'}); };
      if (!record(result) || result.schema !== SCHEMA || result.domain !== 'code') invalid();
      const pipelineStages = result.pipeline_stages === undefined ? [] : result.pipeline_stages;
      if (result.pipeline_stages !== undefined && (query.operation !== 'pipeline' || result.operation !== 'pipeline'
          || !Array.isArray(pipelineStages) || !pipelineStages.length || pipelineStages.length > query.stages.length
          || pipelineStages.some((stage,index) => !record(stage) || stage.index !== index+1
            || stage.operation !== query.stages[index].operation || !['complete','truncated','unavailable'].includes(stage.state)
            || (stage.state === 'unavailable') !== (!!result.error && index+1 === pipelineStages.length)
            || (stage.state === 'complete') !== (stage.reason === null)
            || (stage.reason !== null && (typeof stage.reason !== 'string' || stage.reason.length > 120))
            || (stage.inspected_records !== null && (!Number.isSafeInteger(stage.inspected_records) || stage.inspected_records < 0))
            || !Number.isSafeInteger(stage.hits) || stage.hits < 0 || stage.hits > 200))) invalid();
      if (query.operation === 'pipeline' && ((!result.error && pipelineStages.length !== query.stages.length) || result.next_cursor)) invalid();
      if (result.error && (!record(result.error) || typeof result.error.code !== 'string' || typeof result.error.message !== 'string')) invalid();
      const pipelinePartial = query.operation === 'pipeline' && pipelineStages.length > 1 && validReceipt(result.receipt);
      // Tiny-budget failures may omit success collections. A pipeline with a receipt can retain its prior successful stage.
      if (result.error && !pipelinePartial) {
        data = { ...prior, operation:query.operation, status:result.error.code === 'cancelled' ? 'cancelled' : 'error',
          error:clone(result.error), pipeline_stages:clone(pipelineStages), next_cursor:null };
        changed(snapshot()); return false;
      }
      if (!['hits','edges','paths','counts','omissions','capabilities','channels','operation_capabilities','reasons'].every(key=>Array.isArray(result[key]))
          || typeof result.partial !== 'boolean' || typeof result.degraded !== 'boolean' || !record(result.cost)
          || !validFacets(result.facets) || !(result.next_cursor===null || typeof result.next_cursor==='string' && result.next_cursor.length>0)
          || result.edges.some(edge=>!validEdge(edge))
          || result.paths.some(path=>!record(path) || !Array.isArray(path.node_ids) || !path.node_ids.every(canonicalId) || !Array.isArray(path.edges) || !path.edges.every(validEdge))
          || result.counts.some(row=>!record(row) || !['exact','unknown'].includes(row.exactness) || typeof row.unit!=='string' || typeof row.name!=='string' || !('value' in row) || (row.value!==null && (!Number.isSafeInteger(row.value) || row.value<0)))) invalid();
      if (Number.isSafeInteger(query.budgets?.rows) && codeFacetCount(result.facets)>query.budgets.rows) invalid();
      if (query.operation === 'import_analysis' ? !validImportAnalysis(result.analysis) : result.analysis != null) invalid();
      if(query.operation==='backward_slice' ? !validSlice(result.slice_analysis,query.seed_ids?.[0]) || result.partial!==true || !result.reasons.includes('syntactic_straight_line_only') : result.slice_analysis!=null)invalid();
      if(query.operation==='backward_slice'&&more&&(!prior.slice_analysis||sliceIdentity(prior.slice_analysis)!==sliceIdentity(result.slice_analysis)))invalid();
      if(query.operation==='error_boundaries' ? !validBoundaries(result.error_boundary_analysis,query.seed_ids?.[0])||result.partial!==true||!result.reasons.includes('syntactic_error_boundaries_only') : result.error_boundary_analysis!=null)invalid();
      if(query.operation==='error_boundaries'&&more&&(!prior.error_boundary_analysis||boundaryIdentity(prior.error_boundary_analysis)!==boundaryIdentity(result.error_boundary_analysis)))invalid();
      if (query.operation === 'import_analysis' && more && (!prior.analysis || ANALYSIS_COUNTS.some(k => prior.analysis[k] !== result.analysis[k]))) invalid();
      const rawMode=query.operation==='search'&&query.channels?.length===1&&query.channels[0]==='raw'&&query.read==null;
      const rawFileMatches=result.hits.filter(hit=>hit?.kind==='raw_file_match');
      if(rawFileMatches.some(hit=>!rawMode||!/^raw:[0-9a-f]{64}$/i.test(hit.id)||typeof hit.label!=='string'||typeof hit.properties?.rel_path!=='string'||hit.label!==hit.properties.rel_path||hit.properties.match_mode!=='literal'||hit.properties.scope!=='admitted_files_current_bytes'||hit.properties.destination?.provider!=='host'||typeof hit.properties.destination.resource?.ref!=='string'||hit.current_source_verified!==false||hit.evidence_state!=='observed_file_match'))invalid();
      const rawWindows = result.hits.filter(hit => typeof hit?.id === 'string' && (hit.id.startsWith('raw:') || canonicalId(hit.id) && hit.id.startsWith('file:')) && hit.kind === 'raw_window' && ['read','search'].includes(query.operation));
      const canonicalHits = result.hits.filter(hit => !rawWindows.includes(hit)&&!rawFileMatches.includes(hit));
      const window = result.source_window;
      if (window != null && (!record(window) || typeof window.rel_path !== 'string' || !Number.isSafeInteger(window.start_byte) || window.start_byte<0 || !Number.isSafeInteger(window.end_byte) || window.end_byte<window.start_byte || !['full_sha256','progress_only_nonatomic'].includes(window.continuity) || (window.continuation!==null && typeof window.continuation!=='string'))) invalid();
      const independentRaw = !result.receipt && !canonicalHits.length && (rawMode || rawWindows.length || window && ['read','search'].includes(query.operation)) && rawWindows.every(hit=>hit.id.startsWith('raw:')) && !result.edges.length && !(result.paths || []).length;
      if (!independentRaw && !validReceipt(result.receipt)) invalid();
      if ((!independentRaw && result.receipt.workspace_id !== workspaceId) || (more && codeReceiptKey(prior.receipt) !== codeReceiptKey(result.receipt))
          || (!independentRaw && query.expected_receipt && codeReceiptKey(query.expected_receipt) !== codeReceiptKey(result.receipt))) {
        data = { ...prior, status:'stale', facets:{}, facet_returned:0, error:{code:'stale_receipt', message:'Code scope changed; reload this selection.'}, next_cursor:null };
        changed(snapshot()); return false;
      }
      if (canonicalHits.some(hit => !record(hit) || !canonicalId(hit.id) || typeof hit.kind !== 'string')) invalid();
      const nodes = new Map((more ? prior.nodes : []).map(node => [node.id, node]));
      for (const hit of canonicalHits) nodes.set(hit.id, { ...clone(hit), domain:'code' });
      const edges = new Map((more ? prior.edges : []).map(edge => [edge.id, edge]));
      for (const edge of result.edges) edges.set(codeEdgeId(edge), { ...clone(edge), id:codeEdgeId(edge), domain:'code' });
      const paths = new Map((more ? prior.paths : []).map(path=>[JSON.stringify(path),path]));
      for (const path of result.paths) paths.set(JSON.stringify(path),clone(path));
      const mergedFacets=mergeFacets(more?prior.facets:null,result.facets);
      const mergedSlice=result.slice_analysis?{...clone(result.slice_analysis),units:[...(more?prior.slice_analysis.units:[]),...clone(result.slice_analysis.units)]}:null;
      if(mergedSlice){if(!validSlice(mergedSlice,query.seed_ids?.[0]))invalid();if(!result.next_cursor){const ids=new Set(mergedSlice.units.map(u=>u.id));if(!ids.has(mergedSlice.root_definition_id)||mergedSlice.units.some(u=>u.uses.some(v=>!ids.has(v.definition_id))))invalid();}}
      const mergedBoundaries=result.error_boundary_analysis?{...clone(result.error_boundary_analysis),regions:[...(more?prior.error_boundary_analysis.regions:[]),...clone(result.error_boundary_analysis.regions)]}:null;
      if(mergedBoundaries&&!validBoundaries(mergedBoundaries,query.seed_ids?.[0]))invalid();
      const mergedAnalysis = result.analysis ? {...clone(result.analysis), units:[...(more ? prior.analysis.units : []), ...clone(result.analysis.units)]} : null;
      if (mergedAnalysis && mergedAnalysis.units.length > 1000) invalid();
      if(rawFileMatches.length>bounds.nodes||nodes.size>bounds.nodes||edges.size>bounds.edges||paths.size>bounds.paths||codeFacetCount(mergedFacets)>bounds.facets){data={...prior,status:'error',error:{code:'presentation_limit',message:'This page exceeds retained evidence limits. Reduce query scope/page rows or raise the limit within the engine ceiling.'},next_cursor:null};changed(snapshot());return false;}
      data = { ...clone(result), operation:query.operation, status:'ready', query:selection, nodes:[...nodes.values()], edges:[...edges.values()],
        facets:mergedFacets,facet_returned:codeFacetCount(result.facets), analysis:mergedAnalysis,slice_analysis:mergedSlice,error_boundary_analysis:mergedBoundaries,
        raw_file_matches:clone(rawFileMatches),raw_windows:clone(rawWindows), paths:[...paths.values()], returned:{hits:result.hits.length,edges:result.edges.length,paths:result.paths.length}, error:result.error ? clone(result.error) : null };
      // Facets/counts describe the backend filtered corpus, not a sum of repeated pages.
      delete data.hits;
      changed(snapshot()); return true;
    } catch (error) {
      if (!current()) return false;
      data = { ...prior, status:signal.aborted || error?.name === 'AbortError' ? 'cancelled' : 'error',
        error:{code:signal.aborted || error?.name === 'AbortError' ? 'cancelled' : error?.code === 'invalid_evidence' ? 'invalid_evidence' : 'unavailable', message:signal.aborted ? 'Code query cancelled.' : 'Code projection is unavailable.'}, next_cursor:null };
      changed(snapshot()); return false;
    }
  }
  const run = (workspaceId, query, options = {}) => runInternal(workspaceId, query, {more:options.more === true});
  const applyQualifiedImpact = (workspaceId, query, result) => {
    if (!record(query) || query.operation !== 'impact' || query.cursor != null || !validReceipt(query.expected_receipt) || !record(result)) return false;
    return runInternal(workspaceId, query, {qualifiedResult:result});
  };
  return { run, applyQualifiedImpact, dispose, snapshot };
}
