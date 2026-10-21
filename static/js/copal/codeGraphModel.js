// Disposable C04 projection only. No indexing, persistence authority or ambient context.
const SCHEMA = 'open-clank.code-query/v1';
const CHANNELS = new Set(['exact','identifier','fuzzy','lexical','structural','raw','semantic']);
const RELATIONS = new Set(['call','import','value_reference','type_reference','inherits','implements','render','route','overrides']);
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
  async function run(workspaceId, query, { more = false } = {}) {
    if (disposed || !isCurrent()) return false;
    if (query.channels && (!Array.isArray(query.channels) || query.channels.some(channel=>!CHANNELS.has(channel)))) return false;
    const kinds=query.filter?.symbol_kinds;
    if(kinds!==undefined&&(!Array.isArray(kinds)||kinds.length>64||kinds.some(kind=>typeof kind!=='string'||new TextEncoder().encode(kind).length>128)))return false;
    if ((query.seed_ids || []).some(id => !canonicalId(id)) || (query.target_id && !canonicalId(query.target_id))) return false;
    const selection = codeQueryKey(query);
    if(more&&(data.nodes.length>=bounds.nodes||data.edges.length>=bounds.edges||data.paths.length>=bounds.paths||codeFacetCount(data.facets)>=bounds.facets))return false;
    if (more && (!data.next_cursor || !data.receipt || selection !== data.query)) return false;
    const prior = more ? snapshot() : { ...snapshot(), nodes:[], raw_file_matches:[], raw_windows:[], edges:[], paths:[], counts:[], facets:{}, receipt:null, next_cursor:null, returned:0, omissions:[], channels:[] };
    controller?.abort(); controller = new AbortController();
    const ticket = ++serial, signal = controller.signal;
    const current = () => !disposed && ticket === serial && isCurrent();
    data = { ...prior, operation:query.operation, facet_returned:0, status:'loading', error:null }; changed(snapshot());
    const payload = clone(query);
    if (more) { payload.cursor = prior.next_cursor; payload.expected_receipt = clone(prior.receipt); }
    else if (query.expected_receipt) payload.expected_receipt = clone(query.expected_receipt);
    try {
      const result = await request(workspaceId, payload, signal);
      if (!current()) return false;
      const invalid = () => { throw Object.assign(new Error('Invalid code projection'),{code:'invalid_evidence'}); };
      if (!record(result) || result.schema !== SCHEMA || result.domain !== 'code') invalid();
      // Tiny-budget C04 failures intentionally omit success collections.
      if (result.error) {
        if (!record(result.error) || typeof result.error.code !== 'string' || typeof result.error.message !== 'string') invalid();
        data = { ...prior, status:result.error.code === 'cancelled' ? 'cancelled' : 'error', error:clone(result.error), next_cursor:null };
        changed(snapshot()); return false;
      }
      if (!['hits','edges','paths','counts','omissions','capabilities','channels','operation_capabilities','reasons'].every(key=>Array.isArray(result[key]))
          || typeof result.partial !== 'boolean' || typeof result.degraded !== 'boolean' || !record(result.cost)
          || !validFacets(result.facets) || !(result.next_cursor===null || typeof result.next_cursor==='string' && result.next_cursor.length>0)
          || result.edges.some(edge=>!validEdge(edge))
          || result.paths.some(path=>!record(path) || !Array.isArray(path.node_ids) || !path.node_ids.every(canonicalId) || !Array.isArray(path.edges) || !path.edges.every(validEdge))
          || result.counts.some(row=>!record(row) || !['exact','unknown'].includes(row.exactness) || typeof row.unit!=='string' || typeof row.name!=='string' || !('value' in row) || (row.value!==null && (!Number.isSafeInteger(row.value) || row.value<0)))) invalid();
      if (Number.isSafeInteger(query.budgets?.rows) && codeFacetCount(result.facets)>query.budgets.rows) invalid();
      const rawMode=query.operation==='search'&&query.channels?.length===1&&query.channels[0]==='raw'&&query.read==null;
      const rawFileMatches=result.hits.filter(hit=>hit?.kind==='raw_file_match');
      if(rawFileMatches.some(hit=>!rawMode||!/^raw:[0-9a-f]{64}$/i.test(hit.id)||typeof hit.label!=='string'||typeof hit.properties?.rel_path!=='string'||hit.label!==hit.properties.rel_path||hit.properties.match_mode!=='literal'||hit.properties.scope!=='admitted_files_current_bytes'||hit.properties.destination?.provider!=='host'||typeof hit.properties.destination.resource?.ref!=='string'||hit.current_source_verified!==false||hit.evidence_state!=='observed_file_match'))invalid();
      const rawWindows = result.hits.filter(hit => typeof hit?.id === 'string' && (hit.id.startsWith('raw:') || canonicalId(hit.id) && hit.id.startsWith('file:')) && hit.kind === 'raw_window' && ['read','search'].includes(query.operation));
      const canonicalHits = result.hits.filter(hit => !rawWindows.includes(hit)&&!rawFileMatches.includes(hit));
      const window = result.source_window;
      if (window != null && (!record(window) || typeof window.rel_path !== 'string' || !Number.isSafeInteger(window.start_byte) || window.start_byte<0 || !Number.isSafeInteger(window.end_byte) || window.end_byte<window.start_byte || !['full_sha256','progress_only_nonatomic'].includes(window.continuity) || (window.continuation!==null && typeof window.continuation!=='string'))) invalid();
      const independentRaw = !result.receipt && !canonicalHits.length && (rawMode || rawWindows.length || window && ['read','search'].includes(query.operation)) && rawWindows.every(hit=>hit.id.startsWith('raw:')) && !result.edges.length && !(result.paths || []).length;
      if (!independentRaw && !validReceipt(result.receipt)) invalid();
      if ((!independentRaw && result.receipt.workspace_id !== workspaceId) || (more && codeReceiptKey(prior.receipt) !== codeReceiptKey(result.receipt))) {
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
      if(rawFileMatches.length>bounds.nodes||nodes.size>bounds.nodes||edges.size>bounds.edges||paths.size>bounds.paths||codeFacetCount(mergedFacets)>bounds.facets){data={...prior,status:'error',error:{code:'presentation_limit',message:'This page exceeds retained evidence limits. Reduce query scope/page rows or raise the limit within the engine ceiling.'},next_cursor:null};changed(snapshot());return false;}
      data = { ...clone(result), operation:query.operation, status:'ready', query:selection, nodes:[...nodes.values()], edges:[...edges.values()],
        facets:mergedFacets,facet_returned:codeFacetCount(result.facets),
        raw_file_matches:clone(rawFileMatches),raw_windows:clone(rawWindows), paths:[...paths.values()], returned:{hits:result.hits.length,edges:result.edges.length,paths:result.paths.length}, error:null };
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
  return { run, dispose, snapshot };
}
