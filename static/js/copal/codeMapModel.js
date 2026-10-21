// App-only compact geometry. Neither visual IDs nor preview are evidence.
export const MAP_SCHEMA = 'open-clank.code-map/v1';
export const MAP_RELATIONS = ['containment','import','call','value_reference','type_reference','inherits','implements','render','route','overrides'];
const RELATIONS=MAP_RELATIONS;
const RECEIPT = ['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'];
const ERROR_CODES = ['invalid','invalid_schema','denied','erased','missing','stale_cursor','reindex_required','budget_exceeded','busy','cancelled','unsupported','unavailable','invalid_evidence'];
const ERROR_CODE = Symbol('validated map error');
const encoder = new TextEncoder();
const bytes = value => encoder.encode(JSON.stringify(value)).length;
const copy = value => structuredClone(value);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function need(ok) { if (!ok) throw new Error('Invalid Code Map projection'); }
function keys(value, required, optional=[]) {
  need(object(value) && required.every(k=>Object.hasOwn(value,k)) && Object.keys(value).every(k=>required.includes(k)||optional.includes(k)));
}
function text(value,max=256,nullable=false) { need(nullable&&value===null || typeof value==='string'&&value.length>0&&encoder.encode(value).length<=max&&!/[\u0000-\u001f\u007f-\u009f]/u.test(value)); }
function integer(value,min=0,nullable=false) { need(nullable&&value===null || Number.isSafeInteger(value)&&value>=min); }
function bool(value) { need(typeof value==='boolean'); }
function receipt(value) { keys(value,RECEIPT); for(const k of RECEIPT) ['admission_epoch','policy_generation'].includes(k)?integer(value[k]):text(value[k]); }
function canonical(value,kind='file|decl|occ') { text(value); need(new RegExp(`^(${kind}):[0-9a-fA-F]{1,128}$`).test(value)); }
function authority(value) {
  if(value?.variant==='structure_preview') {
    keys(value,['variant','workspace_id','registration_id','checkout_id','admission_epoch','policy_generation','workspace_revision','selection_policy_digest','inventory_id']);
    for(const k of Object.keys(value)) ['admission_epoch','policy_generation','workspace_revision'].includes(k)?integer(value[k]):text(value[k]);
  } else {
    keys(value,['variant','receipt','selection_policy_digest','summary_id']); need(value.variant==='published'); receipt(value.receipt); text(value.selection_policy_digest); text(value.summary_id);
  }
}
export function codeMapKey(value) {
  if(Array.isArray(value)) return `[${value.map(codeMapKey).join(',')}]`;
  if(object(value)) return `{${Object.keys(value).sort().map(k=>`${JSON.stringify(k)}:${codeMapKey(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}
function equal(a,b) { return codeMapKey(a)===codeMapKey(b); }
function cost(value) {
  keys(value,['inspected_entries','sql_vm_steps','metadata_bytes','retained_summary_bytes','serialized_bytes','source_bytes_read','policy_bytes_read','summary_cache_hit'],['native_rpc_calls_completed']);
  for(const k of Object.keys(value)) k==='summary_cache_hit'?bool(value[k]):integer(value[k]);
  need(value.source_bytes_read===0);
}
function typedFailure(value,mode) {
  if(!object(value)||!Object.hasOwn(value,'error'))return;
  keys(value,['schema','mode','error','cost']);keys(value.error,['code','message']);
  need(bytes(value)<=2048&&value.schema===MAP_SCHEMA&&(value.mode===mode||value.mode===null)&&ERROR_CODES.includes(value.error.code)&&typeof value.error.message==='string');cost(value.cost);
  need(value.cost.serialized_bytes===bytes(value));
  const error=new Error('Code map unavailable');error[ERROR_CODE]=value.error.code;throw error;
}
export function mapRequest(input) {
  keys(input,['schema','operation'],['expected_authority','root','direction','relations','cursor','limits']);
  need(input.schema===MAP_SCHEMA&&['overview','children','neighbors'].includes(input.operation));
  if(Object.hasOwn(input,'limits'))keys(input.limits,[],['bytes','nodes','edges','groups','work']);
  const r={expected_authority:null,root:null,direction:'both',relations:[...RELATIONS],cursor:null,...copy(input),limits:{bytes:262144,nodes:200,edges:800,groups:50,work:4000000,...input.limits}};
  keys(r.limits,['bytes','nodes','edges','groups','work']);
  for(const [k,max] of Object.entries({bytes:262144,nodes:200,edges:800,groups:50,work:4000000})) {integer(r.limits[k],k==='bytes'?512:1);need(r.limits[k]<=max);}
  need(['incoming','outgoing','both'].includes(r.direction)&&Array.isArray(r.relations)&&r.relations.length>0&&r.relations.length<=RELATIONS.length&&new Set(r.relations).size===r.relations.length&&r.relations.every(x=>RELATIONS.includes(x)));
  text(r.cursor,4096,true); if(r.expected_authority!==null)authority(r.expected_authority);
  need(r.operation==='overview'&&r.cursor===null || r.expected_authority!==null);
  if(r.operation==='overview')need(r.root===null);
  else {
    if(r.root?.kind==='visual'){keys(r.root,['kind','id']);text(r.root.id);}
    else {keys(r.root,['kind','resource_id','receipt']);need(r.root.kind==='resource'&&r.operation==='neighbors'&&r.expected_authority?.variant==='published');canonical(r.root.resource_id);receipt(r.root.receipt);need(equal(r.root.receipt,r.expected_authority.receipt));}
  }
  need(bytes(r)<=16384); return r;
}
export function validateMapResult(value,input) {
  const r=mapRequest(input);
  need(bytes(value)<=r.limits.bytes); keys(value,['schema','operation','authority','root','direction','relations','nodes','edges','coverage','page','selection','cost']);
  need(value.schema===MAP_SCHEMA&&value.operation===r.operation&&value.direction===r.direction&&equal(value.relations,r.relations));
  authority(value.authority);need(r.expected_authority===null||equal(value.authority,r.expected_authority));
  const preview=value.authority.variant==='structure_preview',ids=new Map();
  need(Array.isArray(value.nodes)&&value.nodes.length<=r.limits.nodes&&Array.isArray(value.edges)&&value.edges.length<=r.limits.edges);
  for(const n of value.nodes){
    keys(n,['id','label','kind','parent','source_files','asset_files','languages'],['resource_id','file_resource_id','symbol_kind','qualified_name']);text(n.id);text(n.label,160);text(n.parent,256,true);integer(n.source_files);integer(n.asset_files);
    need(!ids.has(n.id)&&n.id!==n.parent&&['repository','folder','file','symbol'].includes(n.kind)&&Array.isArray(n.languages)&&n.languages.length<=8);ids.set(n.id,n);
    if(n.resource_id!==undefined){need(!preview&&['file','symbol'].includes(n.kind));canonical(n.resource_id,n.kind==='file'?'file':'decl|occ');}
    if(n.file_resource_id!==undefined){need(!preview&&n.kind==='symbol');canonical(n.file_resource_id,'file');}
    if(n.symbol_kind!==undefined){need(n.kind==='symbol');text(n.symbol_kind,64);}
    if(n.qualified_name!==undefined){need(n.kind==='symbol');text(n.qualified_name,1024);}
    need(!preview||n.kind!=='symbol');
    const languages=new Set();let sum=0;for(const l of n.languages){keys(l,['language','count']);text(l.language,32);integer(l.count);need(!languages.has(l.language));languages.add(l.language);sum+=l.count;}need(Number.isSafeInteger(sum)&&sum<=n.source_files);
  }
  need(value.nodes.filter(n=>['repository','folder'].includes(n.kind)).length<=r.limits.groups);
  for(const n of value.nodes){const seen=new Set();let id=n.id;while(ids.has(id)){need(!seen.has(id));seen.add(id);id=ids.get(id).parent;}}
  text(value.root,256,true);
  if(r.operation==='overview')need(value.root===null||ids.get(value.root)?.kind==='repository');
  else need(ids.has(value.root)&&(r.root.kind!=='visual'||value.root===r.root.id));
  const edgeIds=new Set();for(const e of value.edges){
    keys(e,['id','from','to','relation','state','count','selection_id']);text(e.id);integer(e.count,1);need(!edgeIds.has(e.id)&&ids.has(e.from)&&ids.has(e.to)&&r.relations.includes(e.relation));edgeIds.add(e.id);
    if(e.relation==='containment')need(e.state==='structure'&&e.selection_id===null&&ids.get(e.to).parent===e.from);
    else {need(!preview&&['exact','inferred'].includes(e.state));text(e.selection_id);}
    if(r.operation==='neighbors')need(r.direction==='incoming'?e.to===value.root:r.direction==='outgoing'?e.from===value.root:e.from===value.root||e.to===value.root);
  }
  const c=value.coverage;keys(c,['inventory_complete','observed_entries','total_entries','indexed_files','parse_failed_files','ignored_entries','unsupported_entries','unavailable_entries','dependencies']);bool(c.inventory_complete);
  for(const k of ['observed_entries','ignored_entries','unsupported_entries','unavailable_entries'])integer(c[k]);for(const k of ['total_entries','indexed_files','parse_failed_files'])integer(c[k],0,true);
  need(c.total_entries===null||c.total_entries>=c.observed_entries);need(!c.inventory_complete||c.total_entries===c.observed_entries);
  if(preview)need(c.indexed_files===null&&c.parse_failed_files===null&&c.dependencies==='indexing'&&value.selection===null);
  else need(c.indexed_files!==null&&c.parse_failed_files!==null&&['complete','partial'].includes(c.dependencies)&&(c.dependencies!=='complete'||c.parse_failed_files===0&&c.unavailable_entries===0));
  if(value.selection!==null){const s=value.selection;keys(s,['visual_id','resource_id','receipt']);canonical(s.resource_id);receipt(s.receipt);need(!preview&&ids.has(s.visual_id)&&equal(s.receipt,value.authority.receipt)&&(value.root===null||s.visual_id===value.root));}
  if(r.root?.kind==='resource')need(value.selection!==null&&value.selection.visual_id===value.root);
  const p=value.page;keys(p,['returned_nodes','returned_edges','remaining_nodes','remaining_edges','complete','next_cursor','truncated']);integer(p.returned_nodes);integer(p.returned_edges);integer(p.remaining_nodes,0,true);integer(p.remaining_edges,0,true);bool(p.complete);bool(p.truncated);text(p.next_cursor,4096,true);
  need(p.returned_nodes===value.nodes.length&&p.returned_edges===value.edges.length&&(p.next_cursor===null||p.next_cursor!==r.cursor));
  need(p.complete?!p.truncated&&p.next_cursor===null&&p.remaining_nodes===0&&p.remaining_edges===0:p.next_cursor!==null&&(p.remaining_nodes!==0||p.remaining_edges!==0));
  cost(value.cost);need(value.cost.serialized_bytes===bytes(value));return copy(value);
}

// One foreground request owns a serial. Geometry is bounded independently from
// native coverage; eviction invalidates all adjacency completion assertions.
export function createCodeMapModel({request,getScope,isCurrent=()=>true,getPinned=()=>[],changed=()=>{},maxNodes=2000,maxEdges=5000,maxBytes=4*1024*1024}) {
  need(typeof request==='function'&&typeof getScope==='function');
  need(Number.isSafeInteger(maxNodes)&&maxNodes>0&&maxNodes<=2000&&Number.isSafeInteger(maxEdges)&&maxEdges>0&&maxEdges<=5000&&Number.isSafeInteger(maxBytes)&&maxBytes>=512&&maxBytes<=16*1024*1024);
  let serial=0,controller=null,disposed=false,scope=null,sequence=null,aggregateSequence=null;
  let state=empty();const nodes=new Map(),edges=new Map(),proofs=new Map(),sequences=new Map(),bindings=new Map();
  function empty(){return {status:'idle',authority:null,nodes:[],edges:[],root:null,page:null,coverage:null,selection:null,aggregate:null,error:null,evicted:false,retainedBytes:0,evidenceEnabled:false};}
  function currentScope(){try{const value=getScope();need(object(value)&&bytes(value)<=16384);return codeMapKey(value);}catch{return null;}}
  const snapshot=()=>{if(scope!==null&&(scope!==currentScope()||!isCurrent())){serial++;controller?.abort();scope=null;clear();}return copy(state);};
  const publish=()=>changed(snapshot());
  function clear(){nodes.clear();edges.clear();proofs.clear();sequences.clear();bindings.clear();sequence=null;aggregateSequence=null;state=empty();}
  function reset(){serial++;controller?.abort();controller=null;scope=null;clear();publish();}
  function dispose(){reset();disposed=true;}
  function begin(workspace){
    if(disposed||!isCurrent())return null;
    const next=currentScope();if(next===null){reset();return null;}if(next!==scope){clear();scope=next;}
    controller?.abort();controller=new AbortController();const ticket=++serial;
    return {signal:controller.signal,current:()=>!disposed&&ticket===serial&&scope===currentScope()&&isCurrent(),workspace};
  }
  function proofKey(r,a){return codeMapKey({authority:a,root:r.root,direction:r.direction,relations:r.relations});}
  function trim(protectedIds){
    for(const id of getPinned().slice(0,64))if(nodes.has(id))protectedIds.add(id);
    // Include all retained IDs/cursors/selection references, not just geometry.
    // Reserve 32 bytes for the accounting counter's own decimal width.
    const size=()=>32+bytes({...state,scope,retainedBytes:0,nodes:[...nodes.values()],edges:[...edges.values()],proofs:[...proofs.values()],aggregateSequence,sequences:[...sequences.values()].map(s=>({workspace:s.workspace,scope:s.scope,query:s.query,cursor:s.cursor,page:s.page,authority:s.authority,seen:[...s.seen],pageNodes:[...s.pageNodes],pageEdges:[...s.pageEdges]})),bindings:[...bindings.values()],sequence:sequence&&{workspace:sequence.workspace,scope:sequence.scope,query:sequence.query,cursor:sequence.cursor,seen:[...sequence.seen],pageNodes:[...sequence.pageNodes],pageEdges:[...sequence.pageEdges]}});
    let evicted=false;
    while(size()>maxBytes&&sequences.size){const key=[...sequences.keys()].find(k=>sequences.get(k)!==sequence)||sequences.keys().next().value;sequences.delete(key);if(sequence?.query===key){sequence.seen.clear();sequence.pageNodes.clear();sequence.pageEdges.clear();sequence.cursor=null;}evicted=true;}
    while(nodes.size>maxNodes||edges.size>maxEdges||size()>maxBytes){
      // Node pressure removes one old unprotected vertex and its incident
      // edges, not every useful relationship before touching a vertex.
      if(nodes.size>maxNodes){
        const id=[...nodes.keys()].find(id=>!protectedIds.has(id));
        if(id===undefined)throw new Error('Code Map cache capacity exceeded');
        nodes.delete(id);bindings.delete(id);for(const [key,e] of edges)if(e.from===id||e.to===id)edges.delete(key);
        evicted=true;continue;
      }
      const edge=[...edges.keys()][0];
      if(edge!==undefined){edges.delete(edge);evicted=true;continue;}
      const id=[...nodes.keys()].find(id=>!protectedIds.has(id));
      if(id===undefined)throw new Error('Code Map cache capacity exceeded');
      nodes.delete(id);bindings.delete(id);evicted=true;
    }
    if(evicted){proofs.clear();sequences.clear();for(const id of bindings.keys())if(!nodes.has(id))bindings.delete(id);state.evicted=true;}
    state.nodes=[...nodes.values()];state.edges=[...edges.values()];state.retainedBytes=size();return evicted;
  }
  async function run(workspace,input,{more=false,missingIsAbsent=false}={}){
    let r;try{r=mapRequest(input);}catch{return false;}
    const query=codeMapKey({...r,cursor:null,expected_authority:null});
    if(more)sequence=sequences.get(query);
    if(more&&(!sequence||sequence.workspace!==workspace||sequence.query!==query||!sequence.cursor||sequence.scope!==currentScope()))return false;
    if(!more&&r.cursor!==null)return false;
    if(more){if(!equal(sequence.authority,state.authority))return false;r={...r,cursor:sequence.cursor,expected_authority:copy(sequence.authority)};}
    const job=begin(workspace);if(!job)return false;
    if(!more){sequence={workspace,query,scope,cursor:null,page:null,authority:copy(r.expected_authority),seen:new Set(),pageNodes:new Set(),pageEdges:new Set()};sequences.delete(query);while(sequences.size>=64)sequences.delete(sequences.keys().next().value);sequences.set(query,sequence);}
    const previous={state:copy(state),nodes:new Map(nodes),edges:new Map(edges),bindings:new Map(bindings),proofs:new Map(proofs)};
    state.status='loading';state.error=null;publish();
    try{
      const raw=await request(workspace,{mode:'map',request:copy(r)},job.signal);if(!job.current())return false;
      typedFailure(raw,'map');
      const value=validateMapResult(raw,r);
      if(value.authority.variant==='structure_preview')need(value.authority.workspace_id===workspace);else need(value.authority.receipt.workspace_id===workspace);
      const changedAuthority=state.authority!==null&&!equal(state.authority,value.authority);
      need(!more||!changedAuthority);
      if(changedAuthority){nodes.clear();edges.clear();proofs.clear();sequences.clear();bindings.clear();sequences.set(query,sequence);state=empty();}
      // A new root/filter sequence replaces its proof, not every other root's.
      if(!more)proofs.delete(proofKey(r,value.authority));
      const cursor=value.page.next_cursor;need(cursor===null||!sequence.seen.has(cursor));need(sequence.seen.size<1000);
      if(cursor!==null)sequence.seen.add(cursor);
      let novel=0;
      for(const [rows,cache,seen] of [[value.nodes,nodes,sequence.pageNodes],[value.edges,edges,sequence.pageEdges]])for(const row of rows){
        if(cache.has(row.id))need(equal(cache.get(row.id),row));
        if(!seen.has(row.id)){novel++;seen.add(row.id);}cache.delete(row.id);cache.set(row.id,row);
      }
      need(sequence.pageNodes.size<=2000&&sequence.pageEdges.size<=5000);need(!more||novel>0||value.page.complete);
      for(const n of nodes.values()){const seen=new Set();let id=n.id;while(nodes.has(id)){need(!seen.has(id));seen.add(id);id=nodes.get(id).parent;}}
      aggregateSequence=null;
      state={...state,authority:value.authority,root:value.root,page:value.page,coverage:value.coverage,selection:value.selection,aggregate:null,status:'ready',error:null,evidenceEnabled:value.authority.variant==='published'};
      sequence.authority=copy(value.authority);sequence.cursor=cursor;sequence.page=copy(value.page);
      if(value.authority.variant==='published')for(const n of value.nodes)if(n.resource_id)bindings.set(n.id,{visual_id:n.id,resource_id:n.resource_id,receipt:copy(value.authority.receipt)});
      if(value.selection)bindings.set(value.selection.visual_id,copy(value.selection));
      const protectedIds=new Set();if(value.root!==null)protectedIds.add(value.root);
      const evicted=trim(protectedIds);
      if(r.operation==='neighbors'&&value.page.complete&&!value.page.truncated&&!evicted&&!state.evicted)proofs.set(proofKey(r,value.authority),{authority:value.authority,root:r.root,direction:r.direction,relations:r.relations});
      // Include proof bytes in the same bound; any eviction clears proofs.
      trim(protectedIds);publish();return true;
    }catch(error){if(!job.current())return false;if(['denied','erased'].includes(error?.[ERROR_CODE]))clear();else{state=previous.state;for(const [target,source]of [[nodes,previous.nodes],[edges,previous.edges],[bindings,previous.bindings],[proofs,previous.proofs]]){target.clear();for(const [k,v]of source)target.set(k,v);}sequences.clear();sequence=null;aggregateSequence=null;}
      if(missingIsAbsent&&r.root?.kind==='resource'&&r.operation==='neighbors'&&!more&&error?.[ERROR_CODE]==='missing'&&state.authority?.variant==='published'){state.status='ready';state.error=null;publish();return false;}
      state.status='error';state.error={code:error?.[ERROR_CODE]||'invalid_or_unavailable',message:'Code Map could not be loaded'};publish();return false;}
  }
  async function selectAggregate(selectionId,{cursor=null}={}){
    snapshot();
    if(state.authority?.variant!=='published'||!state.edges.some(e=>e.selection_id===selectionId))return false;
    if(cursor!==null&&(state.aggregate?.selection_id!==selectionId||state.aggregate.next_cursor!==cursor||!aggregateSequence||aggregateSequence.references.length>=50))return false;
    text(selectionId);text(cursor,4096,true);
    const a=copy(state.authority),workspace=a.receipt.workspace_id,job=begin(workspace);if(!job)return false;
    const prior=cursor===null?{cursors:[],references:[]}:copy(aggregateSequence);
    const r={schema:MAP_SCHEMA,authority:a,selection_id:selectionId,cursor,max_refs:50-prior.references.length};
    try{
      const v=await request(workspace,{mode:'selection',request:r},job.signal);if(!job.current())return false;
      typedFailure(v,'selection');
      keys(v,['schema','authority','selection_id','receipt','references','remaining','complete','next_cursor','cost']);
      need(bytes(v)<=65536&&v.schema===MAP_SCHEMA&&equal(v.authority,a)&&equal(v.receipt,a.receipt)&&v.selection_id===selectionId&&Array.isArray(v.references)&&v.references.length<=r.max_refs);
      const seen=new Set(prior.references.map(ref=>ref.occurrence_id));for(const ref of v.references){keys(ref,['occurrence_id','caller_id','target_id','relation','state']);canonical(ref.occurrence_id,'occ');canonical(ref.target_id,'decl');if(ref.caller_id!==null)canonical(ref.caller_id,'decl');need(!seen.has(ref.occurrence_id)&&RELATIONS.filter(r=>r!=='containment').includes(ref.relation)&&['exact','inferred'].includes(ref.state));seen.add(ref.occurrence_id);}
      integer(v.remaining);bool(v.complete);text(v.next_cursor,4096,true);need(v.next_cursor===null||v.next_cursor!==cursor);need(v.complete===(v.next_cursor===null&&v.remaining===0));need(v.complete||v.references.length>0&&v.next_cursor!==null&&v.remaining>0);cost(v.cost);need(v.cost.serialized_bytes===bytes(v));
      need(v.next_cursor===null||!prior.cursors.includes(v.next_cursor));
      aggregateSequence={references:[...prior.references,...copy(v.references)],cursors:[...prior.cursors,...(v.next_cursor===null?[]:[v.next_cursor])]};
      state.aggregate={...copy(v),references:copy(aggregateSequence.references),clientLimited:aggregateSequence.references.length===50&&!v.complete};trim(new Set(state.root===null?[]:[state.root]));publish();return true;
    }catch(error){if(job.current()){if(['denied','erased'].includes(error?.[ERROR_CODE]))clear();state.status='error';state.error={code:error?.[ERROR_CODE]||'invalid_or_unavailable',message:'Code Map selection could not be loaded'};publish();}return false;}
  }
  return {run,selectAggregate,snapshot,reset,dispose,
    configure(limits={}){serial++;controller?.abort();controller=null;
      maxNodes=Math.max(200,Math.min(2000,limits.maxNodes||maxNodes));maxEdges=Math.max(800,Math.min(5000,limits.maxEdges||maxEdges));maxBytes=Math.max(1048576,Math.min(16777216,limits.maxBytes||maxBytes));
      sequences.clear();sequence=null;proofs.clear();trim(new Set());publish();},
    bindingFor:id=>{snapshot();return nodes.has(id)&&bindings.has(id)?copy(bindings.get(id)):null;},
    pageFor:input=>{snapshot();try{const r=mapRequest(input),key=codeMapKey({...r,cursor:null,expected_authority:null});return copy(sequences.get(key)?.page||null);}catch{return null;}},
    hasCompleteAdjacency:r=>{snapshot();return state.authority!==null&&scope===currentScope()&&proofs.has(proofKey(mapRequest(r),state.authority));}};
}
