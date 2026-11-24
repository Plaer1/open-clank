import { graphBudgetSummary } from './graphBudgetSummary.js';
// Disposable presentation over C04. No source/index/authority persistence.
import { createCodeGraphModel, codeReceiptKey, codeFacetCount, codeQueryKey } from './codeGraphModel.js';
import { indexPresentation } from './codeIndexObserver.js';
import { mountCodeMapWorkbench } from './codeMapWorkbench.js';
import { captureCodeFragment, encodeCodeFragment, decodeCodeFragment, compareCodeFragments } from './codeGraphFragments.js';

export const WORKBENCH_LIMITS = Object.freeze({ mounted:200, loaded:1000, mountedEdges:400, loadedEdges:1000,
  loadedPaths:100, loadedFacetBuckets:1000, inspectorWitnesses:50, breadcrumbPaths:10, breadcrumbNodes:80 });
// A verification run does not invalidate its compatible published head.
// This permits metadata queries only; actual query receipts and current-source
// actions still pass the native and Files authority checks.
export function evidencePublishedHeadKey(index, workspaceId) {
  if(!index||!['ready','failed','admitted','queued','building','running'].includes(index.lifecycle)
    ||['unsupported','reindex_required','invalid_response','invalid_evidence','stale_receipt','stale_context'].includes(index.error?.code)
    ||index.workspace_id!==workspaceId)return null;
  const strings=['workspace_id','registration_id','checkout_id','head_generation','config_fingerprint'];
  const epochs=['admission_epoch','policy_generation','workspace_revision'];
  if(!strings.every(key=>typeof index[key]==='string'&&index[key].length>0)
    ||!epochs.every(key=>Number.isSafeInteger(index[key])&&index[key]>=(key==='workspace_revision'?1:0)))return null;
  // source_snapshot_id is supplied by the query receipt, not lifecycle status.
  return JSON.stringify([...strings,...epochs].map(key=>[key,index[key]]));
}
export function codeCanContinue(data,limits=WORKBENCH_LIMITS) {
  return Boolean(data.status==='ready' && data.next_cursor && codeFacetCount(data.facets)<limits.loadedFacetBuckets
    && data.nodes.length<limits.loaded && data.edges.length<limits.loadedEdges && data.paths.length<limits.loadedPaths
    && (data.analysis?.units?.length ?? 0)<1000 && (data.slice_analysis?.units?.length ?? 0)<64 && (data.error_boundary_analysis?.regions?.length ?? 0)<64);
}
export function anchorZoom(camera, point, factor) {
  const scale = Math.max(.25, Math.min(4, camera.scale * factor));
  const ratio = scale / camera.scale;
  return { x:point.x-(point.x-camera.x)*ratio, y:point.y-(point.y-camera.y)*ratio, scale };
}
export function codePosition(index) { return { x:60+(index%10)*112, y:35+Math.floor(index/10)*30 }; }
export function assignCodePositions(nodes,positions) {
  const used=new Set(),key=point=>`${point.x}:${point.y}`;
  for(const node of nodes){const point=positions.get(node.id);if(point&&!used.has(key(point)))used.add(key(point));else positions.delete(node.id);}
  let slot=0;
  for(const node of nodes)if(!positions.has(node.id)){while(used.has(key(codePosition(slot))))slot++;const point=codePosition(slot++);positions.set(node.id,point);used.add(key(point));}
}
export function capabilityFor(data, operation) {
  const cap=data.operation_capabilities?.find(row=>row.operation===operation);
  return {available:cap?.available===true,reason:cap?.reason || 'Not available in this scope.'};
}
const fields=['label','source','signature','documentation','properties','resolution','witnesses'];
const el=(tag,attrs={},...children)=>{
  const element=document.createElement(tag);
  for(const [key,value] of Object.entries(attrs)) {
    if(key==='text')element.textContent=value;
    else if(key==='class')element.className=value;
    else if(key.startsWith('on'))element.addEventListener(key.slice(2),value);
    else if(value!==false && value!=null)element.setAttribute(key,value===true?'':String(value));
  }
  element.append(...children);return element;
};
const button=(text,fn,attrs={})=>el('button',{type:'button',class:'copal-btn',text,onclick:fn,...attrs});

export function mountCodeGraphWorkbench(options) {
  if (options.requestMap) return mountCodeMapWorkbench({...options, mountEvidence:mountCodeGraphWorkbench});
  const {element,isCurrent,workspaceId='',setWorkspace=()=>{},chooseWorkspace,
    workspaceLabel='',subscribeIndex,request,onController=()=>{},onEvidenceAction,readPreference,writePreference,readResourceCapabilities,initialQuery=true}=options;
  const limits=()=>{const s=options.readGraphSettings?.()||{};return {...WORKBENCH_LIMITS,mounted:s.evidenceMounted||200,loaded:s.evidenceLoaded||1000,mountedEdges:s.evidenceMountedEdges||400,loadedEdges:s.evidenceLoadedEdges||1000,loadedPaths:s.evidencePaths||100,loadedFacetBuckets:s.evidenceFacets||1000,inspectorWitnesses:s.evidenceWitnesses||50,breadcrumbPaths:s.breadcrumbPaths||10,breadcrumbNodes:s.breadcrumbNodes||80};};
  let disposed=false, workspace=workspaceId, model, data={nodes:[],edges:[],paths:[]}, selected=null;
  let camera={x:0,y:0,scale:1}, positions=new Map(), actionSerial=0, query=null, preferenceSerial=0, windowStart=0, qualifiedImpact=false;
  let resourceCapability={available:false,reason:'Current source bridge not verified.'},capabilitySerial=0;
  let layoutReceipt=null;
  let resourceAborter=new AbortController();
  let recordedFragment=null, fragmentSerial=0, fragmentComparedStamp=null, indexAuthorityKey=null, fragmentPaging=false;
  let selectionAborter=new AbortController();
  let comparisonOffer=null, pendingComparisonNavigation=null;
  let indexRelease=null,indexSerial=0,folderSerial=0,indexState=indexPresentation({lifecycle:'missing'}),label=workspaceLabel;
  const aborter=new AbortController(), signal=aborter.signal;
  const observer=new MutationObserver(()=>{if(!element.isConnected)dispose();});
  observer.observe(document.body,{childList:true,subtree:true});
  const current=()=>!disposed && isCurrent() && element.isConnected;
  const listen=(target,type,fn,options={})=>target.addEventListener(type,fn,{...options,signal});
  element.classList.add('code-workbench');element.dataset.codeGraph='true';
  const folder=button('Choose code folder',async()=>{
    const ticket=++folderSerial;
    indexStatus.textContent='Resolving code folder…';
    try {const binding=await chooseWorkspace?.();if(current() && ticket===folderSerial){if(binding?.id)selectWorkspace(binding.id,{label:binding.name || binding.path || binding.id});else paintIndex();}}
    catch(error) {if(current() && ticket===folderSerial)indexStatus.textContent=error.message || 'Workspace selection unavailable.';}
  });
  const scope=el('span',{'data-code-workspace':true,text:label || workspace || 'No code workspace selected'});
  const search=el('input',{type:'search','aria-label':'Search scoped code',placeholder:'Search symbols, identifiers or source…',maxlength:1024});
  const searchMode=el('select',{'aria-label':'Code search mode'},el('option',{value:'metadata',text:'Indexed metadata'}),el('option',{value:'source',text:'Source contents · literal'}),el('option',{value:'semantic',text:'Semantic ranking'}));
  const semanticControls=el('section',{hidden:true,'aria-label':'Semantic search permission'});
  const semanticStatus=el('p',{role:'status'});
  let semanticRoute=null, semanticEnabled=false, semanticRequest=0, semanticAborter=null;
  const semanticConsent=button('Allow semantic search for this route',()=>void updateSemanticPermission(true));
  const semanticDisable=button('Turn off semantic search',()=>void updateSemanticPermission(false));
  semanticControls.append(semanticStatus,semanticConsent,semanticDisable);
  const vectorPanel=el('section',{hidden:true,'aria-label':'Semantic index'});
  const vectorStatus=el('p',{role:'status'});
  const vectorTitle=el('strong',{text:'Semantic index'});
  const vectorReview=el('p',{hidden:true});
  const vectorCheck=button('Check index',()=>void readVectorIndex('status'));
  const vectorPrepare=button('Review next batch',()=>void readVectorIndex('preview'),{hidden:true});
  const vectorBuild=button('Build reviewed batch',()=>void readVectorIndex('build'),{hidden:true});
  const vectorCancel=button('Cancel review',()=>resetVectorReview(),{hidden:true});
  let vectorRequest=null, vectorBatch=null, vectorCanContinue=false;
  vectorPanel.append(vectorTitle,vectorStatus,vectorReview,vectorCheck,vectorPrepare,vectorBuild,vectorCancel);
  semanticControls.append(vectorPanel);

  function resetVectorReview(){
    vectorBatch=null;
    vectorReview.hidden=true;
    vectorReview.textContent='';
    vectorBuild.hidden=vectorCancel.hidden=true;
    vectorPrepare.hidden=!vectorCanContinue;
  }

  function resetVectorIndex(){
    vectorRequest?.abort();vectorRequest=null;
    vectorCanContinue=false;
    vectorCheck.disabled=false;
    vectorStatus.textContent='';
    vectorPanel.hidden=true;
    resetVectorReview();
  }

  async function readVectorIndex(action){
    if(!current()||!workspace||searchMode.value!=='semantic'||!semanticEnabled||vectorRequest)return;
    if(action==='build'&&!vectorBatch)return;
    const capturedWorkspace=workspace, fingerprint=semanticRoute.route_fingerprint;
    const controller=vectorRequest=new AbortController();
    const owns=()=>current()&&workspace===capturedWorkspace&&semanticEnabled&&
      semanticRoute?.route_fingerprint===fingerprint&&searchMode.value==='semantic'&&vectorRequest===controller&&!controller.signal.aborted;
    vectorCheck.disabled=vectorPrepare.disabled=vectorBuild.disabled=vectorCancel.disabled=true;
    const batch=vectorBatch;
    resetVectorReview();
    vectorStatus.textContent=action==='build'?'Building the reviewed batch…':action==='preview'?'Preparing a source and dispatch estimate…':'Reading index status…';
    try{
      const response=await fetch('/api/prefs/code-vector-index',{method:'POST',credentials:'same-origin',cache:'no-store',
        headers:{'Content-Type':'application/json'},signal:controller.signal,
        body:JSON.stringify({workspace_id:capturedWorkspace,action,...(action==='build'?{expected_batch:batch}:{})})});
      if(!response.ok)throw new Error();
      const value=await response.json();
      if(!owns())return;
      if(!['unavailable','partial','ready','pending'].includes(value?.state))throw new Error();
      vectorCanContinue=value.can_continue===true&&value.state!=='pending';
      vectorPrepare.hidden=!vectorCanContinue;
      const count = key => Number.isSafeInteger(value[key])&&value[key]>=0?value[key]:null;
      const indexed=count('indexed'), visited=count('visited'), omitted=count('omitted');
      const labels={unavailable:'Semantic index unavailable.',partial:'Semantic index coverage is partial.',ready:'Semantic index ready for this scope.',pending:'A reviewed batch is pending. Check its status to continue.'};
      vectorStatus.textContent=[labels[value.state],indexed!==null?indexed+' indexed':null,
        visited!==null?visited+' visited':null,omitted!==null&&omitted>0?omitted+' omitted':null,
        value.batch_changed===true?'This batch changed. Review the next batch again.':null].filter(Boolean).join(' · ');
      if(action==='preview'&&typeof value.batch_id==='string'&&value.batch_id.length>0&&value.batch_id.length<=4096&&
          Number.isSafeInteger(value.cost?.excerpts)&&value.cost.excerpts>=0&&value.cost.excerpts<=6&&
          Number.isSafeInteger(value.cost?.max_source_bytes)&&value.cost.max_source_bytes>=0&&
          value.cost.max_source_bytes<=value.cost.excerpts*8192&&
          (value.cost.excerpts===0)===(value.cost.max_source_bytes===0)){
        vectorBatch=value.batch_id;
        vectorReview.textContent=value.cost.excerpts===0
          ?'This batch can advance using local evidence. It sends no source to the embedding route.'
          :`This batch can send ${value.cost.excerpts} current source excerpts, up to ${value.cost.max_source_bytes.toLocaleString()} total source bytes, to ${semanticRoute.provider} · ${semanticRoute.model}. The remaining cost is unknown.`;
        vectorReview.hidden=vectorBuild.hidden=vectorCancel.hidden=false;
        vectorPrepare.hidden=true;
      }
    }catch{
      if(owns())vectorStatus.textContent=action==='build'?
        'Batch outcome unavailable. Check index status before reviewing another dispatch.':'Semantic index is unavailable for this workspace.';
      if(owns()&&action==='build'){vectorCanContinue=false;vectorPrepare.hidden=true;}
    }finally{
      if(owns()){
        vectorRequest=null;
        vectorCheck.disabled=vectorPrepare.disabled=vectorBuild.disabled=vectorCancel.disabled=false;
      }
    }
  }

  function setSemanticStatus(value){
    const configured=value?.configured;
    semanticRoute=validSemanticRoute(configured)?configured:null;
    const consent=value?.consent;
    semanticEnabled=value?.enabled===true&&semanticRoute!==null&&
      ['provider','model','route_id','connection_id','route_fingerprint'].every(key=>semanticRoute[key]===consent?.[key]);
    const provider=semanticRoute?.provider, model=semanticRoute?.model;
    const disclosure=value?.disclosure;
    const max=Number.isSafeInteger(disclosure?.max_excerpts)?disclosure.max_excerpts:null;
    const bytes=Number.isSafeInteger(disclosure?.max_excerpt_bytes)?disclosure.max_excerpt_bytes:null;
    const inputs=Array.isArray(disclosure?.inputs)?disclosure.inputs.filter(v=>typeof v==='string').join(', '):disclosure?.inputs;
    const storage=disclosure?.storage;
    semanticStatus.textContent=semanticRoute?
      [`Embedding route: ${provider} · ${model}`,typeof inputs==='string'?'Inputs: '+inputs:null,
        max!==null&&bytes!==null?`At most ${max} source excerpts, ${bytes} bytes each`:null,
        typeof storage==='string'?'Storage: '+storage:null,
        semanticEnabled?'Enabled for explicit semantic searches':value?.enabled===true?'The configured route differs from your previous permission. Review this route before using it.':'Permission required for this route'].filter(Boolean).join('. '):
      'Configure a managed embedding route to use semantic search.';
    semanticConsent.hidden=semanticEnabled||!semanticRoute||typeof inputs!=='string'||typeof storage!=='string';
    semanticConsent.disabled=semanticConsent.hidden;
    semanticDisable.hidden=value?.enabled!==true;
    semanticDisable.disabled=semanticDisable.hidden;
    vectorPanel.hidden=!semanticEnabled||!workspace;
    if(!vectorPanel.hidden){vectorStatus.textContent='Check the current durable semantic index.';void readVectorIndex('status');}
    updateActions();
  }
  function validSemanticRoute(value){
    return value&&['provider','model','route_id','connection_id','route_fingerprint'].every(key=>typeof value[key]==='string'&&value[key].length>0);
  }
  async function updateSemanticPermission(enabled){
    const serial=++semanticRequest, mode=searchMode.value, workspaceID=workspace;
    const owns=()=>current()&&serial===semanticRequest&&workspace===workspaceID&&mode==='semantic'&&searchMode.value===mode;
    const expectedRoute=semanticRoute;
    semanticAborter?.abort();
    const controller=semanticAborter=new AbortController();
    semanticEnabled=false;
    resetVectorIndex();
    semanticConsent.disabled=semanticDisable.disabled=true;
    semanticStatus.textContent=enabled==null?'Reading semantic search permission…':'Updating semantic search permission…';
    updateActions();
    try{
      const options={credentials:'same-origin',cache:'no-store',signal:controller.signal};
      if(enabled!=null){
        options.method='PUT';
        options.headers={'Content-Type':'application/json'};
        options.body=JSON.stringify({enabled,expected_route:enabled?expectedRoute:null});
      }
      const response=await fetch('/api/prefs/code-vector-search',options);
      if(!response.ok){
        if(response.status===409&&enabled!=null&&owns()){await updateSemanticPermission();return;}
        throw new Error();
      }
      const value=await response.json();
      if(owns())setSemanticStatus(value);
    }catch{
      if(owns()){
        semanticRoute=null;
        semanticConsent.hidden=semanticDisable.hidden=true;
        semanticStatus.textContent='Semantic search permission is unavailable. Choose another search mode or select this mode again.';
      }
    }
  }

  const pathPrefix=el('input',{type:'text',maxlength:1024,'aria-label':'Relative path prefix',placeholder:'Relative path prefix (optional)'});
  const searchButton=button('Search',()=>run('search'));
  const overview=button('Overview',()=>run('overview'),{'data-code-operation':'overview'});
  const symbolKinds=el('input',{type:'text',maxlength:256,'aria-label':'Symbol kinds (comma separated)',placeholder:'Symbol kinds: function, class…'});
  const catalog=button('Files',()=>run('catalog'),{'data-code-operation':'catalog'});
  const folderFacets=button('Folders & filters',()=>run('facets'),{'data-code-operation':'facets'});
  const symbolTab=button('Symbols',()=>{searchMode.value='metadata';semanticAborter?.abort();resetVectorIndex();semanticControls.hidden=true;return run('search',{query:search.value || undefined,filter:{...filters(),kinds:['declaration']}});},{'data-code-operation':'search'});
  const preference=el('input',{type:'checkbox','aria-label':'Automatic code context',disabled:true});
  const preferenceStatus=el('span',{text:'Automatic context is off until verified.'});
  const header=el('header',{class:'code-workbench-header'},el('div',{},el('strong',{text:'Code Graph'}),scope),folder,
    el('label',{class:'code-context-setting'},preference,' Automatic code context',preferenceStatus));
  const indexStatus=el('p',{role:'status','aria-live':'polite','data-code-index-status':true,text:'Choose a code folder to attach its index. Chat workspace and automatic context stay unchanged.'});
  const recheck=button('Recheck index',()=>indexRelease?.refresh?.());
  const retry=button('Retry indexing',()=>startIndex());
  const readmit=button('Readmit erased code index',()=>{
    if(!current() || indexState.lifecycle!=='erased')return;
    indexState=indexPresentation({lifecycle:'starting'});paintIndex();void indexRelease?.readmit?.();
  });
  const detach=button('Stop observing index',()=>{
    indexSerial++;actionSerial++;capabilitySerial++;resourceAborter.abort();indexRelease?.();indexRelease=null;model?.dispose();install();
    indexState=indexPresentation({lifecycle:'unavailable',error:{code:'view_detached',message:'This view detached; other consumers and their builds were not cancelled.'}});paintIndex();
  });
  const indexControls=el('div',{class:'code-workbench-tabs','aria-label':'Code index lifecycle'},recheck,retry,readmit,detach);
  const status=el('p',{role:'status','aria-live':'polite','data-code-status':true,text:'No code query yet.'});
  const counts=el('p',{'data-code-counts':true,class:'code-workbench-counts'});
  const budgetText=el('p',{'data-code-budget-summary':true});
  const budgetDetails=el('details',{},el('summary',{text:'Coverage and budgets'}),budgetText);
  const facets=Object.fromEntries(['folders','languages','states','kinds'].map(key=>[key,el('select',{'aria-label':`Code ${key}`},el('option',{value:'',text:`All ${key}`}))]));
  const pipelineMode = el('select', {'aria-label':'Stages after finding indexed metadata'},
    ...[['find','Find'],['outgoing','Find → outgoing dependencies'],['incoming','Find → incoming dependencies'],
      ['both','Find → incoming and outgoing dependencies'],['outline','Find → limit to one result → outline']]
      .map(([value,text])=>el('option',{value,text})));
  const pipelineLimit = el('input',{type:'number',min:1,max:200,step:1,'aria-label':'Final result limit (optional)',placeholder:'Optional result limit'});
  let pipelineRecord = null;
  const pipelineRun = button('Run pipeline',async()=>{
    pipelineRecord = null;
    const text=search.value.trim();
    if(!text || new TextEncoder().encode(text).length>1024){status.textContent='Enter bounded search text for Find.';return;}
    const maximum=pipelineLimit.valueAsNumber;
    if(pipelineLimit.value && (!Number.isInteger(maximum)||maximum<1||maximum>200)){status.textContent='Choose a result limit from 1 to 200.';return;}
    const filter={...filters(),...(pathPrefix.value.trim()?{path_prefix:pathPrefix.value.trim()}:{}),
      ...(symbolKinds.value.trim()?{symbol_kinds:[...new Set(symbolKinds.value.split(',').map(s=>s.trim()).filter(Boolean))].slice(0,16)}:{})};
    const stages=[{operation:'find',query:text,filter}];
    if(pipelineMode.value==='outline')stages.push({operation:'limit',limit:1},{operation:'outline'});
    else if(['incoming','outgoing','both'].includes(pipelineMode.value))stages.push({operation:'deps',direction:pipelineMode.value});
    if(pipelineLimit.value)stages.push({operation:'limit',limit:maximum});
    const settings = {text, mode:pipelineMode.value, limit:pipelineLimit.value,
      prefix:pathPrefix.value, kinds:symbolKinds.value,
      facets:Object.fromEntries(Object.entries(facets).map(([key,field])=>[key,field.value]))};
    const origin = workspace;
    const runResult = run('pipeline',{query:undefined,filter:{},stages,channels:['exact','identifier','fuzzy','lexical'],
      ...(data.receipt?{expected_receipt:structuredClone(data.receipt)}:{})});
    const ticket = actionSerial;
    await runResult;
    if (current() && origin === workspace && actionSerial === ticket && data.receipt?.workspace_id === workspace
        && data.operation === 'pipeline' && data.pipeline_stages?.length) {
      pipelineRecord = {schema:'open-clank.graph-audit/v1', workspace, settings,
        receipt:structuredClone(data.receipt), stages:structuredClone(data.pipeline_stages),
        partial:Boolean(data.partial || data.error || data.next_cursor),
        resources:data.nodes.slice(0,200).map(n=>n.id), omitted:Math.max(0,data.nodes.length-200)};
    }
  });
  const auditFile = el('input',{type:'file',accept:'application/json,.json',hidden:true});
  const auditStatus = el('p',{role:'status'});
  const saveAudit = button('Export audit with search text',()=>{
    if (!current() || !pipelineRecord || pipelineRecord.workspace !== workspace || !indexState.usable
        || data.operation !== 'pipeline' || codeReceiptKey(pipelineRecord.receipt) !== codeReceiptKey(data.receipt)) {
      auditStatus.textContent='Run the pipeline to record its stages and evidence.'; return;
    }
    const url = URL.createObjectURL(new Blob([JSON.stringify(pipelineRecord)],{type:'application/json'}));
    const link = document.createElement('a'); link.href=url; link.download='open-clank-graph-audit.json';
    link.click(); setTimeout(()=>URL.revokeObjectURL(url),0);
  });
  const loadAudit = button('Review saved audit',()=>{auditFile.value='';auditFile.click();});
  listen(auditFile,'change',async()=>{
    const file=auditFile.files?.[0], origin=workspace, serial=actionSerial;
    if (!file) return;
    try {
      if (file.size>256*1024) throw Error('Audit exceeds 256 KiB.');
      const record=JSON.parse(await file.text());
      if (!current() || origin!==workspace || serial!==actionSerial) return;
      const saved=record.settings, text=(v,max)=>typeof v==='string' && new TextEncoder().encode(v).length<=max;
      if (record.schema!=='open-clank.graph-audit/v1' || record.workspace!==workspace
          || record.receipt?.workspace_id!==workspace || !saved || !text(saved.text,1024) || !saved.text.trim()
          || !['find','outline','outgoing','incoming','both'].includes(saved.mode)
          || !(saved.limit==='' || /^[1-9]\d{0,2}$/.test(saved.limit) && Number(saved.limit)<=200)
          || !text(saved.prefix,1024) || !text(saved.kinds,2048) || !saved.facets
          || !Object.entries(facets).every(([k,f])=>saved.facets[k]==='' || Array.from(f.options).some(o=>o.value===saved.facets[k])))
        throw Error('Audit options or workspace unavailable.');
      search.value=saved.text;searchMode.value='metadata';pipelineMode.value=saved.mode;
      pipelineLimit.value=saved.limit;pathPrefix.value=saved.prefix;symbolKinds.value=saved.kinds;
      for (const [k,f] of Object.entries(facets)) f.value=saved.facets[k];
      pipelineRecord=null;
      auditStatus.textContent='Loaded search and stage options. Review them, then run against the current index. Recorded generation: '+String(record.receipt.head_generation).slice(0,128);
    } catch (error) { if(current() && origin===workspace) auditStatus.textContent=error.message || 'Invalid saved audit.'; }
  });
  const pipeline = el('details',{},el('summary',{text:'Query pipeline'}),
    el('label',{},'Stages ',pipelineMode),el('label',{},'Final result limit ',pipelineLimit),pipelineRun,saveAudit,loadAudit,auditFile,auditStatus);
  const pipelineReport=el('ol',{'aria-label':'Pipeline stages',role:'status',hidden:true});
  const rawPaths=el('section',{'aria-label':'Current file path matches'});
  const list=el('div',{class:'code-workbench-list',role:'listbox','aria-label':'Code resources','data-code-list':true});
  const more=button('Load more',()=>run(null,{more:true}),{disabled:true});
  const previousWindow=button('Previous resources',()=>{windowStart=Math.max(0,windowStart-limits().mounted);show(data);});
  const nextWindow=button('Next resources',()=>{if(windowStart+limits().mounted<(data.nodes.length||data.raw_file_matches?.length||0)){windowStart+=limits().mounted;show(data);}});
  const windowLabel=el('p',{'data-code-window':true});
  const explorer=el('aside',{class:'code-workbench-explorer','aria-label':'Code explorer'},
    el('h3',{text:'Explore'}),el('div',{class:'code-workbench-search'},searchMode,search,searchButton,pathPrefix,symbolKinds),semanticControls,
    ...(options.menuIntegrated?[]:[el('div',{class:'code-workbench-tabs',role:'group','aria-label':'Code scope'},overview,catalog,symbolTab,folderFacets)]),
    ...Object.values(facets),pipeline,pipelineReport,rawPaths,list,el('div',{class:'code-workbench-tabs'},previousWindow,nextWindow),windowLabel,more);
  const viewport=el('div',{class:'code-workbench-viewport','data-code-viewport':true,tabindex:0,role:'region','aria-label':'Code graph. Arrow keys pan; plus and minus zoom.'});
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1200 700');svg.setAttribute('aria-hidden','true');
  const defs=document.createElementNS(svg.namespaceURI,'defs'),marker=document.createElementNS(svg.namespaceURI,'marker');
  const markerId=`code-arrow-${crypto.randomUUID()}`;for(const [key,value]of Object.entries({id:markerId,viewBox:'0 0 10 10',refX:'18',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto-start-reverse'}))marker.setAttribute(key,value);
  const arrow=document.createElementNS(svg.namespaceURI,'path');arrow.setAttribute('d','M 0 0 L 10 5 L 0 10 z');arrow.setAttribute('fill','context-stroke');marker.append(arrow);defs.append(marker);svg.append(defs);
  const world=document.createElementNS(svg.namespaceURI,'g');svg.append(world);viewport.append(svg);
  const impactNotice=el('p',{'data-recorded-impact':true,hidden:true,text:'Impact of indexed declarations in one recorded file; not exact changed declarations or runtime causality.'});
  const legend=el('p',{class:'code-workbench-legend',text:'● File   ◆ Declaration   ○ Occurrence · solid: exact static binding · dashed: inferred/unresolved. Not runtime execution.'});
  const breadcrumbs=el('nav',{'aria-label':'Ordered static path',class:'code-path-breadcrumbs'});
  const reset=button('Reset camera',()=>{camera={x:0,y:0,scale:1};paintCamera();});
  const zoomIn=button('Zoom in',()=>zoom({x:600,y:350},1.2));
  const zoomOut=button('Zoom out',()=>zoom({x:600,y:350},1/1.2));
  const center=el('section',{class:'code-workbench-center'},...(options.menuIntegrated?[]:[el('div',{class:'code-camera-controls'},zoomIn,zoomOut,reset)]),viewport,legend,breadcrumbs);
  const title=el('h3',{text:'Select a resource'}), fact=el('p',{'data-code-fact':true}), source=el('p',{'data-code-source':true});
  const compiler=el('p',{hidden:true,'aria-label':'Compiler source evidence'});
  const scip=el('div',{hidden:true,'aria-label':'SCIP index observations'});
  const detail=el('pre',{'data-code-detail':true,class:'code-workbench-detail'});
  const witnesses=el('div',{'data-code-witnesses':true});
  const actions=el('div',{class:'code-workbench-actions'});
  const fragmentText=el('p',{role:'status'}),fragmentDetailsText=el('pre',{class:'code-workbench-detail'}),fragmentDetails=el('details',{hidden:true},el('summary',{text:'Keyed evidence details'}),fragmentDetailsText),fragmentPanel=el('section',{'aria-label':'Recorded code evidence',hidden:true},el('h3',{text:'Recorded code evidence'}),el('p',{text:'A local user-supplied observation, not authenticated History. Opaque workspace and source-context identifiers are retained. Absence is unknown, not a deletion.'}),fragmentText,fragmentDetails);
  const fragmentStamp=()=>JSON.stringify([workspace,indexAuthorityKey,indexState.usable,data.status,codeReceiptKey(data.receipt),data.query]);
  const fragmentNotice=text=>{fragmentComparedStamp=null;fragmentDetails.hidden=true;fragmentDetailsText.textContent='';fragmentText.textContent=text;};
  const invalidateFragment=()=>{if(fragmentComparedStamp!==null&&fragmentComparedStamp!==fragmentStamp())fragmentNotice('Current evidence changed. Compare again when it is ready.');};
  const fragmentInput=el('input',{type:'file',accept:'application/json,.json',hidden:true});
  const currentFragment=()=>{if(!current()||!indexState.usable||!query||data.query!==codeQueryKey(query))throw Error('Current evidence unavailable');const observed=fragmentPaging?{...data,partial:true,reasons:[...(data.reasons||[]),'unknown']}:data;const fragment=captureCodeFragment(observed,query);if(fragment.receipt.workspace_id!==workspace)throw Error('Workspace changed');return fragment;};
  const downloadFragment=button('Download evidence fragment',()=>{try{const fragment=currentFragment();const url=URL.createObjectURL(new Blob([encodeCodeFragment(fragment)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='code-evidence-fragment.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),0);}catch(error){fragmentPanel.hidden=false;fragmentNotice(error.message||'Evidence unavailable.');}});
  const importFragment=button('Import evidence fragment',()=>{if(current()){fragmentInput.value='';fragmentInput.click();}});
  const compareFragment=button('Compare current fragment',()=>{try{if(!recordedFragment)throw Error('Evidence unavailable');const result=compareCodeFragments(recordedFragment,currentFragment());fragmentPanel.hidden=false;fragmentText.textContent=`Nodes: ${result.nodes.both} keyed matches, ${result.nodes.recorded_only.length} recorded-only, ${result.nodes.current_only.length} current-only; edges: ${result.edges.both} keyed matches, ${result.edges.recorded_only.length} recorded-only, ${result.edges.current_only.length} current-only. Coverage is recorded below. Observed-only keys are not additions or deletions.`;fragmentDetailsText.textContent=JSON.stringify(result,null,2);fragmentDetails.hidden=false;fragmentComparedStamp=fragmentStamp();}catch(error){fragmentPanel.hidden=false;fragmentNotice(error.message||'Comparison unavailable.');}});
  listen(fragmentInput,'change',async()=>{const file=fragmentInput.files?.[0],ticket=++fragmentSerial,originalWorkspace=workspace,originalAction=actionSerial,receipt=codeReceiptKey(data.receipt),authority=indexAuthorityKey;if(!file)return;recordedFragment=null;fragmentPanel.hidden=false;fragmentNotice('Reading selected observation…');const stillCurrent=()=>current()&&ticket===fragmentSerial&&workspace===originalWorkspace&&actionSerial===originalAction&&codeReceiptKey(data.receipt)===receipt&&indexAuthorityKey===authority;if(file.size>1024*1024){fragmentNotice('Fragment exceeds the import limit.');return;}try{const text=await file.text();if(!stillCurrent())return;recordedFragment=decodeCodeFragment(text);fragmentNotice('Saved observation loaded. Compare it explicitly with current evidence.');}catch{if(stillCurrent())fragmentNotice('Invalid or unavailable evidence fragment.');}});
  fragmentPanel.append(compareFragment);actions.append(fragmentInput,...(options.menuIntegrated?[]:[downloadFragment,importFragment]));
  // Explicit user-selected indexed scope. These are observations, never runtime removals.
  const topologyPaths=new Set(), topologyOwner=options.topologyOwner || {};
  let topologyStamp=null, topologyOperation=topologyOwner.operation || null, topologyOperationScope=topologyOwner.workspace || null, topologyBlocked=topologyOwner.blocked===true, topologySerial=0;
  const retainTopology=()=>{topologyOwner.operation=topologyOperation;topologyOwner.workspace=topologyOperationScope;topologyOwner.blocked=topologyBlocked;};
  // Tab-scoped recovery selector only: it grants no account or Files authority.
  const topologyKey=scope=>JSON.stringify(['openclank.graph.topology.original.v1',options.accountID,scope]);
  const restoreTopology=scope=>{
    topologyOperation=null;topologyOperationScope=null;topologyBlocked=false;
    if(!scope || typeof options.accountID!=='string' || !options.accountID){retainTopology();return;}
    try {
      const value=sessionStorage.getItem(topologyKey(scope));
      if(value!==null){topologyBlocked=true;if(/^[0-9a-f]{32}$/.test(value)){topologyOperation=value;topologyOperationScope=scope;}}
    } catch {topologyBlocked=true;}
    retainTopology();
    if(topologyBlocked)topologyText.textContent=topologyOperation?'An original capture is retained in this tab. Use Check capture before another capture.':'Capture recovery storage is unavailable or invalid.';
  };
  const persistTopology=(scope,operation)=>{
    try {
      if(!current()||workspace!==scope||typeof options.accountID!=='string'||!options.accountID||!/^[0-9a-f]{32}$/.test(operation))return false;
      const key=topologyKey(scope), previous=sessionStorage.getItem(key);
      if(previous!==null&&previous!==operation)return false;
      sessionStorage.setItem(key,operation);
      return sessionStorage.getItem(key)===operation;
    } catch {return false;}
  };
  const clearTopology=(scope,operation)=>{
    try {const key=topologyKey(scope),value=sessionStorage.getItem(key);if(value===null)return true;if(value!==operation)return false;sessionStorage.removeItem(key);return sessionStorage.getItem(key)===null;}catch{return false;}
  };
  const topologyText=el('p',{role:'status',text:'Select indexed files to capture their static topology.'});
  const topologyList=el('ul',{'aria-label':'Selected topology files'});
  const topologyDetailsText=el('pre',{class:'code-workbench-detail'}), topologyDetails=el('details',{hidden:true},el('summary',{text:'Indexed comparison details'}),topologyDetailsText);
  const topologyPanel=el('section',{'aria-label':'Recorded indexed topology'},el('h3',{text:'Recorded indexed topology'}),
    el('p',{text:'Selected indexed files only. Capture uses the current published index for the selected paths, including small indexes. Partial extraction and unresolved endpoints remain unknown; differences do not establish runtime behavior or semantic removal.'}),topologyText,topologyList);
  const topologyCurrent=stamp=>current() && indexState.usable && data.status==='ready' && data.receipt && stamp===JSON.stringify([workspace,codeReceiptKey(data.receipt),indexAuthorityKey]);
  const topologyNow=()=>JSON.stringify([workspace,codeReceiptKey(data.receipt),indexAuthorityKey]);
  const drawTopology=()=>{topologyList.replaceChildren(...[...topologyPaths].sort().map(path=>el('li',{},el('span',{text:path}),button('Remove',()=>{topologyPaths.delete(path);topologySerial++;drawTopology();updateActions();}))));};
  const addTopology=button('Add selected file',()=>{const node=data.nodes.find(row=>row.id===selected), path=node?.source?.rel_path || node?.properties?.rel_path;
    if(!current()||!indexState.usable||data.status!=='ready'||!data.receipt||node?.kind!=='file'||typeof path!=='string'||!path)return;
    const stamp=topologyNow();if(topologyStamp!==stamp){topologyPaths.clear();topologyStamp=stamp;}topologyPaths.add(path);topologySerial++;drawTopology();updateActions();topologyText.textContent='Choose all files in the intended comparison scope.';});
  const captureTopology=button('Capture selected topology',async()=>{const stamp=topologyStamp,ticket=++topologySerial;
    const valid=()=>topologyCurrent(stamp)&&ticket===topologySerial; if(!valid()||!topologyPaths.size||topologyBlocked||!options.captureTopology)return;
    const compareBytes=(a,b)=>{const x=new TextEncoder().encode(a),y=new TextEncoder().encode(b);for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length;};
    const paths=[...topologyPaths].sort(compareBytes),receipt=structuredClone(data.receipt),scope=workspace,operation=crypto.randomUUID().replaceAll('-','');
    let armed=false;
    const sameAttempt=()=>current()&&workspace===scope&&ticket===topologySerial&&topologyOperation===operation;
    const finishOriginal=()=>{if(clearTopology(scope,operation)){topologyOperation=null;topologyOperationScope=null;topologyBlocked=false;return true;}topologyBlocked=true;return false;};
    topologyOperation=operation;topologyOperationScope=scope;topologyBlocked=true;retainTopology();updateActions();topologyText.textContent='Choose an authorized destination…';
    try{const result=await options.captureTopology({workspaceId:scope,paths,receipt,operation,isCurrent:valid,persistOriginal:()=>{
        if(!valid())return false;armed=persistTopology(scope,operation);return armed;
      }});
      if(!sameAttempt())return;
      if(!result){if(armed){topologyBlocked=true;topologyText.textContent='Capture outcome unavailable. Use Check original; no retry was issued.';}
        else {restoreTopology(scope);if(!topologyBlocked)topologyText.textContent='Capture cancelled or recovery storage unavailable.';}
        retainTopology();updateActions();return;}
      if(result.operation_id!==operation)throw Error('Capture identity changed');
      if(['saved','unavailable'].includes(result.state))finishOriginal();else topologyBlocked=true;
      retainTopology();updateActions();topologyText.textContent=result.state==='saved'?'Capture saved in History.':`Original capture: ${result.state}. ${topologyBlocked?'Use Check original to inspect its outcome.':''}`;}
    catch{if(sameAttempt()){
      if(armed){topologyBlocked=true;topologyText.textContent='Capture outcome unavailable. Use Check original; no retry was issued.';}
      else {restoreTopology(scope);if(!topologyBlocked)topologyText.textContent='Capture was not issued.';}
      retainTopology();updateActions();}}});
  const checkTopology=button('Check capture',async()=>{const ticket=++topologySerial,operation=topologyOperation,scope=topologyOperationScope,valid=()=>current()&&workspace===scope&&ticket===topologySerial&&topologyOperation===operation;if(!valid()||!operation||!options.topologyStatus)return;
    try{const result=await options.topologyStatus({operation,isCurrent:valid});if(valid()){
      if(['saved','unavailable'].includes(result?.state)&&clearTopology(scope,operation)){topologyOperation=null;topologyOperationScope=null;topologyBlocked=false;}
      retainTopology();updateActions();topologyText.textContent=result?.state==='saved'?'Capture saved in History. Choose its file and version to compare.':`Original capture: ${result?.state||'unknown'}. No retry was issued.`;}}catch{if(valid())topologyText.textContent='Original capture status unavailable.';}});
  const compareTopology=button('Compare a saved topology',()=>{const stamp=topologyNow(),ticket=++topologySerial,scope=workspace,receipt=structuredClone(data.receipt),valid=()=>topologyCurrent(stamp)&&ticket===topologySerial;
    if(!valid()||!options.compareTopology)return; topologyText.textContent='Choose a saved file and explicitly select its History version.';
    options.compareTopology({workspaceId:scope,receipt,isCurrent:valid,onResult:result=>{if(!valid())return;const d=result?.declarations,u=result?.uncertainty,x=result?.uncomparable_relations,paths=result?.scope_paths;if(!d||!u||!x||!Array.isArray(paths))throw Error('Comparison unavailable');topologyDetailsText.textContent=JSON.stringify(result,null,2);topologyDetails.hidden=false;topologyText.textContent=`${paths.length} selected files: ${paths.join(', ')}. Declarations: ${d.matched} matched, ${d.recorded_only.length} recorded-only, ${d.current_only.length} current-only. Indexed relation counts changed for ${result.indexed_relation_changes.length} keys; ${x.recorded_rows} recorded and ${x.current_rows} current relation rows could not be compared. ${result.partial_extraction?'Partial extraction. ':''}Unresolved observations: ${u.recorded} recorded, ${u.current} current. Outside scope, semantic removal and runtime behavior remain unknown.`;},onError:()=>{if(valid())topologyText.textContent='Comparison unavailable or its scope changed.';}});});
  topologyPanel.append(el('div',{class:'code-workbench-actions'},addTopology,...(options.menuIntegrated?[]:[captureTopology,checkTopology,compareTopology])),topologyDetails);

  for(const target of [document,window])for(const event of ['openclank:auth-context-changed','openclank-account-changed'])listen(target,event,()=>{comparisonOffer=null;pendingComparisonNavigation=null;openCompared.hidden=true;libraryPackage.value='';analysisFacts.textContent='';actionSerial++;resourceAborter.abort();resourceAborter=new AbortController();fragmentSerial++;recordedFragment=null;fragmentPanel.hidden=true;fragmentNotice('');});
  const operationButtons=[];
  const addOp=(label,operation,extra={})=>{
    const control=button(label,()=>{if(selected)run(operation,{seed_ids:[selected],...extra});},{'data-code-operation':operation});
    operationButtons.push([control,operation,true]);actions.append(control);return control;
  };
  addOp('Declaration outline','outline');addOp('Incoming','neighbors',{direction:'incoming'});
  addOp('Outgoing','neighbors',{direction:'outgoing'});addOp('Impact','impact',{direction:'incoming'});
  const backwardSlice=button('Backward slice of return',()=>{const node=data.nodes.find(row=>row.id===selected),profile=node?.properties?.backwardSlice;if(profile?.status==='available'&&/^occ:[0-9a-f]{64}$/.test(profile.seed||'')&&data.receipt)run('backward_slice',{query:undefined,filter:{},channels:[],seed_ids:[profile.seed],expected_receipt:structuredClone(data.receipt)});},{'data-code-operation':'backward_slice'});
  actions.append(backwardSlice);
  const errorBoundaries=button('Python error boundaries',()=>{if(/^decl:[0-9a-f]{64}$/.test(selected||'')&&data.receipt)run('error_boundaries',{query:undefined,filter:{},channels:[],seed_ids:[selected],expected_receipt:structuredClone(data.receipt)});},{'data-code-operation':'error_boundaries'});
  actions.append(errorBoundaries);
  const traceTarget=el('select',{'aria-label':'Static path target'},el('option',{value:'',text:'Choose path target…'}));
  const path=button('Trace to',()=>{if(selected && traceTarget.value)run('path',{seed_ids:[selected],target_id:traceTarget.value,direction:'outgoing'});},{'data-code-operation':'path'});
  operationButtons.push([path,'path',true]);actions.append(traceTarget,path);
  const focus=button('Focus selected',()=>{if(selected)run('neighbors',{seed_ids:[selected],direction:'both'});});actions.append(focus);
  const importAnalysis=button('Analyze import cycles & hubs',()=>{
    if(!current() || !data.receipt || data.status!=='ready')return;
    return run('import_analysis',{query:undefined,filter:{},expected_receipt:structuredClone(data.receipt)});
  },{'data-code-operation':'import_analysis'});
  actions.append(importAnalysis); operationButtons.push([importAnalysis,'import_analysis',false]);
  const analysisFacts=el('pre',{'data-code-import-analysis':true});
  const libraryPackage=el('input',{type:'text','aria-label':'Installed npm package',placeholder:'npm package or @scope/package',maxlength:214});
  const libraryLookup=button('Inspect on-disk package API',async()=>{
    if(!current() || !workspace || !options.onLibraryLookup)return;
    const packageName=libraryPackage.value.trim(),scope=workspace,ticket=++actionSerial;
    const valid=()=>current()&&workspace===scope&&actionSerial===ticket&&libraryPackage.value.trim()===packageName;
    if(!/^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/.test(packageName)||packageName.includes('..')){status.textContent='Enter an npm package name.';return;}
    libraryLookup.disabled=true;
    try {const result=await options.onLibraryLookup({workspaceId:scope,packageName,isCurrent:valid,signal:resourceAborter.signal});
      if(!valid())return;
      if(!result?.ok){analysisFacts.textContent='';status.textContent='On-disk package source unavailable.';return;}
      analysisFacts.textContent=result.text;status.textContent='On-disk package declarations observed; exports and runtime loading are not proved.';
    } catch {if(valid()){analysisFacts.textContent='';status.textContent='On-disk package source unavailable.';}}
    finally {if(valid())libraryLookup.disabled=false;}
  },{'data-code-library-lookup':true});
  actions.append(libraryPackage,libraryLookup);
  listen(libraryPackage,'input',()=>{actionSerial++;analysisFacts.textContent='';libraryLookup.disabled=false;});
  const inspectOccurrence=button('Inspect occurrence',()=>{
    if(!selected?.startsWith('occ:')||!data.receipt)return;
    void run('inspect',{seed_ids:[selected],expected_receipt:structuredClone(data.receipt),
      query:undefined,filter:{},channels:[],fields:['source','properties','label','resolution']});
  });
  actions.append(inspectOccurrence);
  const evidenceButtons=[];
  for(const [kind,label] of [['open','Open source'],['add','Add to chat'],['ask','Ask about this'],['summary','Summarize'],['history','Recorded changes'],['compare','Compare local dependency'],['compare_compiler_import','Look up import in another workspace'],['select_scip','Choose SCIP index'],['clear_scip','Clear SCIP index']]){
    const control=button(label,async()=>{
      if(!selected || !current())return;
      const node=data.nodes.find(row=>row.id===selected), receipt=structuredClone(data.receipt), ticket=++actionSerial;
      const key=codeReceiptKey(receipt), id=selected, scope=workspace;
      const valid=()=>current() && ticket===actionSerial && selected===id && workspace===scope && codeReceiptKey(data.receipt)===key;
      control.disabled=true;
      const source=node?.source, span=source?.span;
      const sourceRange=kind==='open'&&['declaration','occurrence'].includes(node?.kind)&&
        Number.isSafeInteger(span?.start_byte)&&Number.isSafeInteger(span?.end_byte)&&
        span.start_byte>=0&&span.end_byte>span.start_byte&&/^[0-9a-f]{64}$/.test(source.source_hash||'')
        ?{startByte:span.start_byte,endByte:span.end_byte,sourceHash:source.source_hash}:undefined;
      try {const result=await onEvidenceAction?.(kind,{node,receipt,workspaceId:scope,isCurrent:valid,signal:AbortSignal.any([resourceAborter.signal,selectionAborter.signal]),sourceRange});if(valid()){if(kind==='compare'||kind==='compare_compiler_import'){comparisonOffer=result?.navigation||null;openCompared.hidden=!comparisonOffer;openCompared.disabled=false;}status.textContent=result?.message || (result?.ok?'Source action completed.':'Current source action unavailable.');
        if ((kind==='select_scip'||kind==='clear_scip') && result?.ok && id.startsWith('occ:')) {
          void run('inspect',{seed_ids:[id],expected_receipt:receipt,query:undefined,filter:{},channels:[],
            fields:['source','properties','label','resolution']});
        }}}
      catch {if(valid())status.textContent='Current source action unavailable.';}
      finally{if(valid())updateActions();}
    });control.dataset.codeEvidenceAction=kind;evidenceButtons.push(control);actions.append(control);
  }
  const openCompared=button('Open target in Graph',async()=>{
    if(!comparisonOffer || !selected || !current())return;
    const offer=comparisonOffer,node=data.nodes.find(row=>row.id===selected),receipt=structuredClone(data.receipt),id=selected,scope=workspace,ticket=++actionSerial;
    const valid=()=>current()&&ticket===actionSerial&&selected===id&&workspace===scope&&comparisonOffer===offer&&codeReceiptKey(data.receipt)===codeReceiptKey(receipt);
    openCompared.disabled=true;
    try {
      const checked=await onEvidenceAction?.('navigate_comparison',{node,receipt,workspaceId:scope,navigation:offer,isCurrent:valid,signal:AbortSignal.any([resourceAborter.signal,selectionAborter.signal])});
      if(!valid())return;
      if(!checked?.ok){status.textContent=checked?.message||'Target source unavailable.';return;}
      pendingComparisonNavigation={workspaceId:offer.workspace_id,id:offer.id||offer.file_id,receipt:structuredClone(offer.receipt),started:false};
      comparisonOffer=null;openCompared.hidden=true;
      selectWorkspace(offer.workspace_id,{label:offer.workspace_id,preserveNavigation:true});
    } catch {if(valid())status.textContent='Target source unavailable.';}
    finally {if(valid())openCompared.disabled=false;}
  });openCompared.hidden=true;actions.append(openCompared);
  if(options.onNavigateResource)actions.append(button('Show in graph',()=>{if(selected)options.onNavigateResource({id:selected,receipt:structuredClone(data.receipt)});}));
  const inspector=el('aside',{class:'code-workbench-inspector','aria-label':'Selected code evidence'},title,fact,source,compiler,scip,actions,analysisFacts,witnesses,detail);
  const diagnostics=el('details',{class:'code-workbench-diagnostics'},el('summary',{text:'Coverage, channels and omissions'}),el('pre',{'data-code-facets':true}));
  element.append(...(options.menuIntegrated?[]:[header,indexStatus,indexControls]),status,impactNotice,counts,budgetDetails,fragmentPanel,topologyPanel,el('div',{class:'code-workbench-panels'},explorer,center,inspector),diagnostics);
  operationButtons.push([overview,'overview',false],[catalog,'catalog',false],[symbolTab,'search',false],[folderFacets,'facets',false],[searchButton,'search',false]);
  function filters(){return {path_prefix:facets.folders.value?`${facets.folders.value}/`:undefined,
    ...Object.fromEntries(['languages','states','kinds'].map(key=>[key,facets[key].value?[facets[key].value]:[]]))};}
  function install(){model=createCodeGraphModel({request,isCurrent:current,changed:show,limits:{nodes:limits().loaded,edges:limits().loadedEdges,paths:limits().loadedPaths,facets:limits().loadedFacetBuckets}});onController(model);show(model.snapshot());}
  async function loadResourceCapabilities(){const ticket=++capabilitySerial,id=workspace;resourceCapability={available:false,reason:'Verifying current source capability.'};
    try{const result=await readResourceCapabilities?.(id,resourceAborter.signal);if(current()&&ticket===capabilitySerial&&id===workspace)resourceCapability=result?.available===true?result:{available:false,reason:result?.reason||'Source bridge unavailable.'};}
    catch{if(current()&&ticket===capabilitySerial)resourceCapability={available:false,reason:'Source bridge unavailable.'};}
    if(current()&&ticket===capabilitySerial)updateActions();
  }
  function paintIndex(){
    invalidateFragment();
    indexStatus.textContent=indexState.text;
    indexStatus.dataset.recovering=String(indexState.recovering===true);
    readmit.hidden=indexState.lifecycle!=='erased';readmit.disabled=!indexRelease;
    retry.hidden=!['missing','failed','cancelled','unavailable'].includes(indexState.lifecycle);
    retry.disabled=!workspace;
    recheck.disabled=!indexRelease || indexState.pending;
    detach.disabled=!indexRelease;
    updateActions();
  }
  function startIndex(){
    const ticket=++indexSerial,id=workspace;indexRelease?.();indexRelease=null;
    if(!id || !current())return;
    actionSerial++;capabilitySerial++;resourceAborter.abort();resourceAborter=new AbortController();selected=null;model?.dispose();install();query=null;qualifiedImpact=false;impactNotice.hidden=true;
    title.textContent='Select a resource';fact.textContent='';source.textContent='';compiler.hidden=true;scip.hidden=true;scip.replaceChildren();detail.textContent='';witnesses.replaceChildren();
    indexState=indexPresentation({lifecycle:'starting'});paintIndex();
    if(!subscribeIndex){indexState=indexPresentation({lifecycle:'unavailable'});paintIndex();return;}
    let loadedHead=null;
    const subscription=subscribeIndex(id,{isCurrent:()=>current()&&ticket===indexSerial&&workspace===id,onStatus:index=>{
      if(!current()||ticket!==indexSerial||workspace!==id)return;
      const headKey=evidencePublishedHeadKey(index,workspace);
      indexAuthorityKey=headKey;
      indexState={...indexPresentation(index),usable:headKey!==null};paintIndex();
      const changedHead=loadedHead!==null&&headKey!==null&&loadedHead!==headKey;
      if(changedHead||!indexState.usable&&!(query?.channels?.includes('raw')&&!['denied','erased'].includes(indexState.lifecycle))){loadedHead=null;actionSerial++;capabilitySerial++;resourceAborter.abort();resourceAborter=new AbortController();selected=null;model?.dispose();install();query=null;}
      if(indexState.usable&&loadedHead!==headKey){loadedHead=headKey;void loadResourceCapabilities();if(pendingComparisonNavigation?.workspaceId===workspace&&!pendingComparisonNavigation.started){pendingComparisonNavigation.started=true;run('inspect',{query:undefined,filter:{},channels:[],seed_ids:[pendingComparisonNavigation.id],expected_receipt:pendingComparisonNavigation.receipt});}else if(initialQuery)run('overview');}
    }});
    if(!current()||ticket!==indexSerial)subscription?.();else indexRelease=subscription;
    paintIndex();
  }
  function selectWorkspace(id,{label:nextLabel=id,preserveNavigation=false}={}){if(!current())return;libraryLookup.disabled=false;if(!preserveNavigation)pendingComparisonNavigation=null;comparisonOffer=null;openCompared.hidden=true;folderSerial++;actionSerial++;if(workspace!==id){fragmentSerial++;recordedFragment=null;fragmentPanel.hidden=true;for(const control of Object.values(facets))control.value='';}workspace=id;if(searchMode.value==='semantic')void updateSemanticPermission();topologySerial++;restoreTopology(id);label=nextLabel;setWorkspace(id,label);scope.textContent=label;
    selected=null;windowStart=0;layoutReceipt=null;camera={x:0,y:0,scale:1};positions.clear();resourceCapability={available:false,reason:'Waiting for an indexed head.'};startIndex();}
  function run(operation,extra={}){
    const semantic=operation==='search'&&searchMode.value==='semantic';
    if(semantic&&(!semanticEnabled||!search.value.trim())){
      if(current())semanticStatus.textContent='Enable semantic search for the configured route and enter a query.';
      return false;
    }
    const raw=searchMode.value==='source'&&(operation==='search'||operation==='read');
    if(!current()||!workspace||!indexState.usable&&!raw||['denied','erased'].includes(indexState.lifecycle))return false;
    actionSerial++;
    if(recordedFragment){fragmentPanel.hidden=false;fragmentNotice('Current evidence changed. Compare again after the query is ready.');}
    if(extra.more){if(qualifiedImpact)return false;fragmentPaging=true;return query && model.run(workspace,query,{more:true});}
    qualifiedImpact=false;impactNotice.hidden=true;
    fragmentPaging=false;
    windowStart=0;
    query={schema:'open-clank.code-query/v1',operation:operation||'overview',query:search.value||undefined,fields,
      filter:{...filters(),...(symbolKinds.value.trim()?{symbol_kinds:[...new Set(symbolKinds.value.split(',').map(s=>s.trim()).filter(Boolean))].slice(0,16)}:{})},budgets:{rows:options.readGraphSettings?.().searchRows||50,bytes:(options.readGraphSettings?.().searchKiB||64)*1024,tokens:options.readGraphSettings?.().searchTokens||8192,hops:options.readGraphSettings?.().searchHops??4,work:options.readGraphSettings?.().queryWork||4000000},
      ...(operation==='search'?{channels:['exact','identifier','fuzzy','lexical',...(semantic?['semantic']:[])]}:{}),...extra};
    if(raw){query.channels=['raw'];query.filter=pathPrefix.value.trim()?{path_prefix:pathPrefix.value.trim()}:{};if(operation==='read')query.fields=[...fields,'excerpt'];}
    else if(!['import_analysis','backward_slice','pipeline','inspect'].includes(operation)&&pathPrefix.value.trim())query.filter.path_prefix=pathPrefix.value.trim();
    return model.run(workspace,query);
  }
  async function inspectResource({id,receipt,workspaceId,isCurrent:originCurrent}){
    if(!current()||!originCurrent()||workspace!==workspaceId||receipt?.workspace_id!==workspaceId||
        !/^(file|decl|occ):[0-9a-f]{64}$/i.test(id||''))return false;
    const originalModel=model;
    const head=indexAuthorityKey;
    selected=null;
    title.textContent='Loading selected evidence…';
    fact.textContent='';source.textContent='';detail.textContent='';witnesses.replaceChildren();
    compiler.hidden=true;scip.hidden=true;scip.replaceChildren();
    const pending=run('inspect',{query:undefined,filter:{},channels:[],seed_ids:[id],expected_receipt:receipt});
    const capturedQuery=query;
    const owns=()=>current()&&originCurrent()&&workspace===workspaceId&&model===originalModel&&
      indexAuthorityKey===head&&query===capturedQuery;
    const ok=await pending;
    if(!owns())return false;
    if(ok&&data.status==='ready'&&data.operation==='inspect'&&codeReceiptKey(data.receipt)===codeReceiptKey(receipt)
        &&data.nodes.some(node=>node.id===id)){
      select(id,{focusList:true});
      return true;
    }
    title.textContent='Selected evidence unavailable';
    status.textContent='The selected resource could not be inspected at the captured index revision.';
    updateActions();
    return false;
  }
  async function applyQualifiedImpact(value, {expectedReceipt,isCurrent:originCurrent} = {}) {
    if (!current() || !indexState.usable || !indexAuthorityKey || typeof originCurrent !== 'function' || originCurrent() !== true
        || value?.workspace_id !== workspace || value?.query?.operation !== 'impact'
        || codeReceiptKey(value?.query?.expected_receipt) !== codeReceiptKey(expectedReceipt)
        || codeReceiptKey(value?.graph?.receipt) !== codeReceiptKey(expectedReceipt)) return false;
    const head=indexAuthorityKey, ticket=actionSerial, originalModel=model;
    if (!current() || !indexState.usable || indexAuthorityKey!==head || ticket!==actionSerial || model!==originalModel || originCurrent()!==true) return false;
    ++actionSerial;
    fragmentPaging=false;windowStart=0;query=structuredClone(value.query);qualifiedImpact=true;impactNotice.hidden=false;
    // Once validation begins its own render may retire the originating selection.
    const applied=await originalModel.applyQualifiedImpact(workspace,query,value.graph);
    if(!applied && current() && model===originalModel){qualifiedImpact=false;impactNotice.hidden=true;}
    return applied;
  }
  function updateActions(){
    const topologyReady=topologyCurrent(topologyStamp);
    addTopology.disabled=!current()||!indexState.usable||data.status!=='ready'||!selected?.startsWith('file:');
    captureTopology.disabled=!topologyReady||!topologyPaths.size||topologyBlocked||!options.captureTopology;
    checkTopology.disabled=!current()||workspace!==topologyOperationScope||!topologyOperation||!options.topologyStatus;
    compareTopology.disabled=!current()||!indexState.usable||data.status!=='ready'||!data.receipt||!options.compareTopology;
    for(const [control,operation,requiresSelection] of operationButtons){const cap=capabilityFor(data,operation);
      control.disabled=!workspace || (operation==='import_analysis'&&!data.receipt) || (!indexState.usable&&!(operation==='search'&&searchMode.value==='source'&&!['denied','erased'].includes(indexState.lifecycle))) || data.status==='loading' || (!cap.available&&data.status==='ready'&&!(operation==='search'&&searchMode.value==='source')) || (requiresSelection&&!selected);
      if(control===symbolTab&&!indexState.usable)control.disabled=true;control.title=cap.available?'':cap.reason;
    }
    for(const control of Object.values(facets))control.disabled=searchMode.value==='source'||!indexState.usable||data.status==='loading';
    search.disabled=!workspace||!indexState.usable&&searchMode.value!=='source';symbolKinds.disabled=searchMode.value==='source';pathPrefix.disabled=!workspace;
    pipelineRun.disabled=!workspace || !indexState.usable || data.status==='loading' || searchMode.value!=='metadata';
    if(searchMode.value==='semantic'&&(!semanticEnabled||!search.value.trim()))searchButton.disabled=true;
    focus.disabled=!indexState.usable || !selected || !capabilityFor(data,'neighbors').available;
    const selectedNode=data.nodes.find(row=>row.id===selected);
    inspectOccurrence.hidden=selectedNode?.kind!=='occurrence';
    inspectOccurrence.disabled=!indexState.usable||data.status!=='ready'||!data.receipt;
    const sliceProfile=selectedNode?.properties?.backwardSlice;
    backwardSlice.disabled=!indexState.usable||data.status!=='ready'||sliceProfile?.status!=='available'||!/^occ:[0-9a-f]{64}$/.test(sliceProfile.seed||'')||!capabilityFor(data,'backward_slice').available;
    errorBoundaries.disabled=!indexState.usable||data.status!=='ready'||!/^decl:[0-9a-f]{64}$/.test(selected||'')||!data.receipt||!capabilityFor(data,'error_boundaries').available;
    backwardSlice.title=backwardSlice.disabled?'A supported indexed return-value profile is required.':'Syntactic local dependencies only; not runtime execution.';
    // These actions use the separately verified source bridge, not native raw
    // reads (deliberately unavailable in the metadata engine: Files owns I/O).
    for(const control of evidenceButtons){const typed=/^(file|decl|occ):[0-9a-f]{64}$/i.test(selected||'');control.disabled=!typed||!onEvidenceAction||data.status!=='ready'||!resourceCapability.available;
      control.disabled ||= !indexState.usable;
      if(control.dataset.codeEvidenceAction==='compare'){control.disabled ||= !selected?.startsWith('file:') || (selectedNode?.source?.rel_path || selectedNode?.properties?.rel_path || selectedNode?.label || '').split('/').at(-1)!=='package.json';}
      if(control.dataset.codeEvidenceAction==='compare_compiler_import'){
        control.hidden=!selected?.startsWith('occ:') || selectedNode?.properties?.language!=='python'
          || selectedNode?.properties?.occurrence_kind!=='call'
          || !['unresolved','unsupported'].includes(selectedNode?.evidence_state);
        control.disabled ||= control.hidden || !data.receipt;
      }
      control.title=!typed?'A canonical indexed resource identity is required.':!resourceCapability.available?resourceCapability.reason:'Revalidate current authorized source before action.';}
  }
  function select(id,{focusList=false}={}){
    if(!current() || !data.nodes.some(row=>row.id===id))return;
    if(selected!==id){comparisonOffer=null;openCompared.hidden=true;selectionAborter.abort();selectionAborter=new AbortController();}
    actionSerial++;selected=id;const node=data.nodes.find(row=>row.id===id);
    title.textContent=node.label||node.id;
    fact.textContent=`${node.evidence_state||'unknown'} · ${node.kind} · ${node.current_source_verified===true?'bounded current-source observation':'metadata; current bytes not verified'}`;
    source.textContent=node.source?`${node.source.rel_path} · lines ${node.source.span?.start_line??'?'}–${node.source.span?.end_line??'?'} · hash ${node.source.source_hash||'unknown'}`:'Source locator omitted or unavailable.';
    const observation=node.properties?.scipObservation;
    const activeScip=observation?.profile==='selected_scip_binary_v1'&&observation.assertion==='index_declared'
      && ['observed','ambiguous','unresolved'].includes(observation.status);
    scip.hidden=!activeScip;
    scip.replaceChildren();
    if(activeScip){
      scip.append(el('p',{text:'SCIP index assertion · '+observation.status
        +(observation.producer?.name?' · '+observation.producer.name:'')}));
      const candidates=Array.isArray(observation.candidates)?observation.candidates.slice(0,6):[];
      for(const candidate of candidates){
        if(!/^file:[a-f0-9]{64}$/.test(candidate?.id||'')||typeof candidate.source?.rel_path!=='string'
            || !Number.isSafeInteger(candidate.source?.span?.start_line))continue;
        scip.append(button(`${candidate.source.rel_path}, line ${candidate.source.span.start_line}`,async()=>{
          const scope=workspace, receipt=structuredClone(data.receipt), ticket=++actionSerial;
          const valid=()=>current()&&selected===id&&workspace===scope&&ticket===actionSerial&&codeReceiptKey(data.receipt)===codeReceiptKey(receipt);
          try{
            const result=await onEvidenceAction?.('open_scip_definition',{node,receipt,workspaceId:scope,scipCandidate:candidate,
              isCurrent:valid,signal:AbortSignal.any([resourceAborter.signal,selectionAborter.signal])});
            if(valid())status.textContent=result?.message||'Definition unavailable.';
          }catch{if(valid())status.textContent='Definition unavailable.';}
        }));
      }
    }
    const compilerEvidence=node.properties?.compilerObservation;
    const compilerStatus=compilerEvidence?.version===1&&compilerEvidence.profile==='python_compiler_scope_v1'&&
      ['qualified','unsupported','unavailable'].includes(compilerEvidence.status)?compilerEvidence.status:null;
    compiler.hidden=!compilerStatus;
    compiler.textContent=compilerStatus==='qualified'?
      ['CPython compiler scope · qualified',...(Array.isArray(compilerEvidence.qualifiers)?compilerEvidence.qualifiers.filter(value=>typeof value==='string').slice(0,12).map(value=>value.slice(0,100)):[])].join(' · '):
      compilerStatus?'CPython compiler scope · '+compilerStatus:'';
    detail.textContent=JSON.stringify(node,null,2);detail.dataset.receipt=JSON.stringify(data.receipt);
    const edges=data.edges.filter(edge=>edge.from===id||edge.to===id);
    witnesses.replaceChildren(el('h4',{text:`Incoming / outgoing witnesses (${edges.length} loaded; ${Math.min(edges.length,limits().inspectorWitnesses)} shown)`}),
      ...edges.slice(0,limits().inspectorWitnesses).map(edge=>el('details',{},el('summary',{text:`${edge.to===id?'Incoming':'Outgoing'} · ${edge.resolution_state} · ${edge.occurrence_id}`}),
        el('pre',{text:JSON.stringify(edge,null,2)}))),
      ...(edges.length>limits().inspectorWitnesses?[el('p',{text:`${edges.length-limits().inspectorWitnesses} loaded witnesses omitted from inspector.`})]:[]));
    for(const control of list.querySelectorAll('[data-code-id]')){const active=control.dataset.codeId===id;control.setAttribute('aria-selected',String(active));control.tabIndex=active?0:-1;if(active&&focusList)control.focus();}
    for(const nodeElement of world.querySelectorAll('[data-code-point]'))nodeElement.classList.toggle('is-selected',nodeElement.dataset.codePoint===id);
    for(const edgeElement of world.querySelectorAll('[data-code-edge]'))edgeElement.classList.toggle('is-adjacent',edgeElement.dataset.from===id||edgeElement.dataset.to===id);
    updateActions();
  }
  function paintCamera(){world.setAttribute('transform',`translate(${camera.x} ${camera.y}) scale(${camera.scale})`);viewport.dataset.camera=JSON.stringify(camera);}
  function zoom(point,factor){camera=anchorZoom(camera,point,factor);paintCamera();}
  function coordinates(event){const matrix=svg.getScreenCTM();if(!matrix)return {x:600,y:350};return new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());}
  function renderGraph(nodes,edges){
    world.replaceChildren();assignCodePositions(nodes,positions);
    for(const edge of edges){const a=positions.get(edge.from),b=positions.get(edge.to);if(!a||!b)continue;
      const line=document.createElementNS(svg.namespaceURI,'line');for(const [key,value] of Object.entries({x1:a.x,y1:a.y,x2:b.x,y2:b.y,'marker-end':`url(#${markerId})`,'data-code-edge':edge.id,'data-from':edge.from,'data-to':edge.to,class:`code-edge ${edge.resolution_state==='exact'?'is-exact':'is-inferred'}`}))line.setAttribute(key,value);world.append(line);}
    for(const node of nodes){const position=positions.get(node.id),group=document.createElementNS(svg.namespaceURI,'g');group.setAttribute('transform',`translate(${position.x} ${position.y})`);group.dataset.codePoint=node.id;group.classList.add('code-point');
      const shape=document.createElementNS(svg.namespaceURI,node.kind==='file'||node.kind==='occurrence'?'circle':'path');
      if(shape.tagName==='circle'){shape.setAttribute('r',node.kind==='file'?9:6);if(node.kind==='occurrence'){shape.style.fill='none';shape.setAttribute('stroke','currentColor');}}
      else{shape.setAttribute('d','M 0 -9 L 9 0 L 0 9 L -9 0 z');shape.setAttribute('fill','currentColor');}group.append(shape);
      const text=document.createElementNS(svg.namespaceURI,'text');text.setAttribute('x','15');text.setAttribute('y','4');text.textContent=String(node.label||node.id).split('/').at(-1).slice(0,24);group.append(text);group.addEventListener('click',()=>select(node.id));world.append(group);}
    paintCamera();
  }
  function show(next){
    if(!current())return;data=next;
    invalidateFragment();
    const capability = operation => { const row = data.operation_capabilities?.find(item => item.operation === operation); return row ? (row.available === true ? 'available' : 'unavailable') : 'unknown'; };
    budgetText.textContent=Object.values(graphBudgetSummary(options.readGraphSettings?.()||{})).join(' ') + ` Incoming/outgoing neighbors: ${capability('neighbors')}; path tracing: ${capability('path')}. Omission notices: ${data.status === 'ready' ? (data.omissions?.length ?? 'unknown') : 'unknown'}.`;
    const analysis=data.status==='ready'&&data.operation==='import_analysis'?data.analysis:null;
    const slice=data.status==='ready'&&data.operation==='backward_slice'?data.slice_analysis:null;
    const boundaries=data.status==='ready'&&data.operation==='error_boundaries'?data.error_boundary_analysis:null;
    analysisFacts.textContent=boundaries ? [`Python try/except syntax · indexed source ${boundaries.source.rel_path}.`, 'Lexical containment only; no exception or execution claim.', `${boundaries.regions.length} regions loaded${data.next_cursor?'; more available':''}.`, ...boundaries.regions.map(region=>`try line ${region.region.start_line} · ${region.handlers.length} handlers · ${region.calls.length} calls in body`)].join('\n') : slice ? [`Python straight-line value dependencies · indexed source ${slice.source.rel_path}.`, 'Approximate syntactic evidence; no runtime, alias, type or purity claim.', `${slice.units.length} definitions loaded${data.next_cursor?'; more available':''}.`, ...slice.units.map(unit=>`${unit.kind}: ${unit.name} · line ${unit.target_span.start_line} · ${unit.uses.length} input uses`)].join('\n') : analysis ? [
      `Indexed exact-import graph: ${analysis.file_count} admitted files, ${analysis.edge_count} unique directed file pairs.`,
      `Excluded: ${analysis.inferred} inferred, ${analysis.ambiguous} ambiguous, ${analysis.unresolved} unresolved, ${analysis.unsupported} unsupported, ${analysis.excluded} outside selection, ${analysis.missing} missing.`,
      `${data.partial?'Partial evidence or additional pages remain.':'All units for this bounded snapshot are shown.'} Degrees apply only to the analyzed file pairs; this is not a live-source or global absence proof.`,
      ...analysis.units.map(unit=>unit.kind==='cycle' ? `Cycle members: ${unit.node_ids.join(', ')} · import witnesses: ${unit.occurrence_ids.join(', ')}` : `Hub: ${unit.node_id} · incoming ${unit.in_degree} · outgoing ${unit.out_degree}`),
    ].join('\n') : '';
    if(data.status==='ready'&&data.receipt){const key=codeReceiptKey(data.receipt);if(layoutReceipt&&layoutReceipt!==key){comparisonOffer=null;openCompared.hidden=true;positions.clear();camera={x:0,y:0,scale:1};selected=null;windowStart=0;}layoutReceipt=key;}
    if(data.status==='ready'){const retained=new Set(data.nodes.map(node=>node.id));for(const id of positions.keys())if(!retained.has(id))positions.delete(id);}
    const facetBuckets=codeFacetCount(data.facets),facetLimit=facetBuckets>=limits().loadedFacetBuckets;
    status.textContent=data.error?`Code query ${data.error.code}: ${data.error.message || 'Recheck index status or choose an authorized folder.'}`:`${data.operation==='facets'?`${data.status} · ${facetBuckets} filter options loaded${data.next_cursor?(facetLimit?' · option limit reached; more remain':' · more filter options available'):data.status==='ready'?' · filter option page stream complete':''}`:data.status==='ready'&&data.operation==='import_analysis'?'Indexed import analysis':data.status==='ready'&&!data.nodes.length?data.raw_file_matches?.length?'Current literal file matches':searchMode.value==='source'?'No current literal file match in the bounded admitted-file scan':'Ready · no indexed code resources':data.status}${data.partial?' · partial':''}${data.degraded?' · degraded':''}${data.reasons?.length?' · '+data.reasons.join(', '):''} · metadata is not verified-current source`;
    if(data.status==='ready'&&data.operation==='search'&&query?.channels?.includes('semantic')){
      const states={
        semantic_ranked_current_page:'Semantic reranking applied within the returned candidate page.',
        semantic_ranked_published_generation:'Ranked against the compatible published declaration index.',
        semantic_published_profile_unavailable:'The current embedding model does not match the published index.',
        semantic_published_index_budget:'The published index exceeds this search budget.',
        semantic_published_index_unavailable:'The published index was unavailable for this search.',
        code_vector_search_opt_in_required:'Embedding route permission is required.',
        code_vector_route_changed:'The managed route changed. Confirm the current route before using semantic search again.',
        code_vector_search_unavailable:'Semantic reranking was unavailable.',
        code_vector_no_matching_declarations:'No eligible declaration excerpts in this candidate page.',
      };
      const reason=(data.reasons||[]).find(code=>Object.hasOwn(states,code));
      status.append(' · '+(reason?states[reason]:'Semantic ranking coverage was not reported for this page.'));
      if(['code_vector_route_changed','code_vector_search_opt_in_required'].includes(reason)&&semanticEnabled)void updateSemanticPermission();
    }
    const pipelineStages=data.operation==='pipeline' ? data.pipeline_stages || [] : [];
    pipelineReport.hidden=pipelineStages.length===0;
    pipelineReport.replaceChildren(...pipelineStages.map(stage=>el('li',{text:[
      `${stage.operation} · ${stage.state}`, `${stage.hits} results`,
      stage.inspected_records===null?'Inspection count unavailable':`${stage.inspected_records} inspected`,
      stage.reason?.replaceAll('_',' '),
    ].filter(Boolean).join(' · ')})));
    windowStart=Math.min(windowStart,Math.max(0,Math.floor(((data.nodes.length||data.raw_file_matches?.length||0)-1)/limits().mounted)*limits().mounted));
    const nodes=data.nodes.slice(windowStart,windowStart+limits().mounted);
    previousWindow.disabled=windowStart===0;nextWindow.disabled=windowStart+limits().mounted>=(data.nodes.length||data.raw_file_matches?.length||0);
    windowLabel.textContent=`Resources ${nodes.length?windowStart+1:0}–${windowStart+nodes.length} of ${data.nodes.length} loaded; backend total is reported separately.`;
    const ids=new Set(nodes.map(node=>node.id)),visibleEdges=data.edges.filter(edge=>ids.has(edge.from)&&ids.has(edge.to)),mountedEdges=visibleEdges.slice(0,limits().mountedEdges);
    counts.textContent=`Returned this page: ${data.returned?.hits||0} hits, ${data.returned?.edges||0} edges, ${data.returned?.paths||0} paths; unique loaded: ${data.nodes.length}; mounted: ${nodes.length}; outside this window: ${Math.max(0,data.nodes.length-nodes.length)} (viewport cap ${limits().mounted}, retained cap ${limits().loaded}). Edges loaded: ${data.edges.length}, mounted: ${mountedEdges.length}, omitted from viewport: ${data.edges.length-mountedEdges.length}; paths loaded: ${data.paths.length}. Continuation stops at ${limits().loaded} resources / ${limits().loadedEdges} edges / ${limits().loadedPaths} paths; an oversized page is rejected without partial evidence. `+(data.counts||[]).map(row=>`${row.name}: ${row.exactness==='exact'&&row.value!=null?row.value:'?'} ${row.unit}`).join('; ');
    list.replaceChildren(...nodes.map((node,index)=>button(`${node.label||node.id} · ${node.kind} · ${node.evidence_state||'unknown'}${node.matches?.some?.(match=>match?.channel==='semantic')?' · semantic match':''}`,()=>select(node.id),{'data-code-id':node.id,role:'option','aria-selected':node.id===selected,tabindex:index===0?0:-1})));
    renderGraph(nodes,mountedEdges);
    const oldTarget=traceTarget.value;traceTarget.replaceChildren(el('option',{value:'',text:'Choose path target…'}),...data.nodes.map(node=>el('option',{value:node.id,text:node.label||node.id})));traceTarget.value=oldTarget;
    if(selected&&!data.nodes.some(node=>node.id===selected)){comparisonOffer=null;openCompared.hidden=true;selected=null;title.textContent='Selection outside this result';fact.textContent='Select a visible resource.';source.textContent='';compiler.hidden=true;scip.hidden=true;scip.replaceChildren();detail.textContent='';witnesses.replaceChildren();}
    if(pendingComparisonNavigation?.started && ['ready','error','stale','cancelled'].includes(data.status) && (data.status!=='ready'||data.operation==='inspect')){
      const nav=pendingComparisonNavigation;pendingComparisonNavigation=null;
      if(data.status==='ready' && codeReceiptKey(data.receipt)===codeReceiptKey(nav.receipt) && data.nodes.some(node=>node.id===nav.id))select(nav.id);
      else status.textContent='Target receipt changed or target is unavailable.';
    }
    if(selected)select(selected);
    if(data.paths?.length){const paths=data.paths.slice(0,limits().breadcrumbPaths),allIds=paths.flatMap(path=>path.node_ids),omittedPaths=data.paths.length-paths.length,omittedNodes=Math.max(0,allIds.length-limits().breadcrumbNodes);
      detail.textContent=JSON.stringify({ordered_paths:paths,omitted_paths:omittedPaths},null,2);breadcrumbs.replaceChildren(...allIds.slice(0,limits().breadcrumbNodes).map(id=>button(data.nodes.find(node=>node.id===id)?.label||id,()=>select(id))),
        el('span',{text:`${omittedPaths} loaded paths / ${omittedNodes} breadcrumb nodes omitted.`}));}
    else breadcrumbs.replaceChildren();
    rawPaths.replaceChildren();if(data.raw_file_matches?.length){rawPaths.append(el('p',{text:`Current admitted files · literal path matches ${windowStart+1}–${Math.min(data.raw_file_matches.length,windowStart+limits().mounted)} of ${data.raw_file_matches.length} loaded. Unknown corpus total; refine the relative path prefix. These are not symbol hits or indexed evidence.`}));for(const hit of data.raw_file_matches.slice(windowStart,windowStart+limits().mounted))rawPaths.append(el('div',{},el('span',{text:hit.label}),button('Open file',async()=>{const ticket=++actionSerial,scope=workspace;const owned=()=>current()&&ticket===actionSerial&&scope===workspace&&data.raw_file_matches?.some(h=>h.id===hit.id);try{if(owned())await options.onRawOpen?.(hit.properties.destination,{isCurrent:owned});}catch{if(owned())status.textContent='Current file could not be opened; recheck folder access or repeat the search.';}}),button('Read current window',()=>{selected=null;return run('read',{channels:['raw'],query:undefined,filter:{},read:{rel_path:hit.properties.rel_path,start_byte:0,length:4096}});})) );}
    if(data.raw_windows?.length||data.source_window)detail.textContent=JSON.stringify({state:'explicit_noncanonical_window',source_window:data.source_window,windows:data.raw_windows},null,2);
    detail.dataset.receipt=JSON.stringify(data.receipt);
    counts.textContent+=` Filter options returned this page: ${data.facet_returned||0}; unique loaded: ${facetBuckets}; continuation limit ${limits().loadedFacetBuckets} buckets across axes (oversized pages rejected).${data.next_cursor&&facetLimit?' More options remain beyond this presentation limit.':data.next_cursor&&data.nodes.length>=limits().loaded?' Use Folders & filters for independent option pages.':''}`;
    more.disabled=qualifiedImpact || !codeCanContinue(data,limits());
    for(const [key,control]of Object.entries(facets)){const value=control.value,rows=data.facets?.[key]||[];control.replaceChildren(el('option',{value:'',text:`All ${key}`}),...rows.map(row=>el('option',{value:row.value,text:`${row.value||'(root)'} · ${row.exactness==='exact'?row.count:'?'} ${row.unit}`})));if(value&&!rows.some(row=>row.value===value))control.append(el('option',{value,text:`${value} · outside result`}));control.value=value;}
    diagnostics.querySelector('pre').textContent=JSON.stringify({facets:data.facets,omissions:data.omissions,channels:data.channels,capabilities:data.capabilities},null,2);updateActions();
  }
  for(const control of Object.values(facets))listen(control,'change',()=>run('catalog'));
  listen(search,'input',()=>updateActions());
  listen(searchMode,'change',()=>{semanticRequest++;semanticAborter?.abort();resetVectorIndex();semanticEnabled=false;semanticRoute=null;semanticControls.hidden=searchMode.value!=='semantic';actionSerial++;resourceAborter.abort();resourceAborter=new AbortController();model.dispose();selected=null;query=null;windowStart=0;detail.textContent='';title.textContent='Select a resource';source.textContent='';compiler.hidden=true;scip.hidden=true;scip.replaceChildren();fact.textContent='';install();search.placeholder=searchMode.value==='source'?'Literal current file contents…':searchMode.value==='semantic'?'Describe the code you are looking for…':'Names, qualified names, signatures, documentation…';updateActions();if(searchMode.value==='semantic')void updateSemanticPermission();});
  listen(pathPrefix,'keydown',event=>{if(event.key==='Enter'){event.preventDefault();run('search');}});
  listen(symbolKinds,'change',()=>run('search'));
  listen(search,'keydown',event=>{if(event.key==='Enter'){event.preventDefault();run('search');}});
  listen(list,'keydown',event=>{const items=[...list.querySelectorAll('[data-code-id]')],at=items.indexOf(document.activeElement);let next=at;
    if(event.key==='ArrowDown')next=Math.min(items.length-1,at+1);else if(event.key==='ArrowUp')next=Math.max(0,at-1);else if(event.key==='Home')next=0;else if(event.key==='End')next=items.length-1;else return;
    event.preventDefault();if(items[next])select(items[next].dataset.codeId,{focusList:true});});
  listen(viewport,'wheel',event=>{event.preventDefault();zoom(coordinates(event),Math.exp(-event.deltaY*.001));},{passive:false});
  const pointers=new Map();let gesture=null;
  listen(viewport,'pointerdown',event=>{if(event.target.closest('[data-code-point]'))return;viewport.setPointerCapture(event.pointerId);pointers.set(event.pointerId,coordinates(event));gesture={camera:{...camera},points:[...pointers.values()]};viewport.focus();});
  listen(viewport,'pointermove',event=>{if(!pointers.has(event.pointerId)||!gesture)return;pointers.set(event.pointerId,coordinates(event));const points=[...pointers.values()];
    if(points.length===1&&gesture.points.length===1){camera={...gesture.camera,x:gesture.camera.x+points[0].x-gesture.points[0].x,y:gesture.camera.y+points[0].y-gesture.points[0].y};paintCamera();}
    else if(points.length===2&&gesture.points.length===2){const distance=p=>Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y),mid=p=>({x:(p[0].x+p[1].x)/2,y:(p[0].y+p[1].y)/2});camera=anchorZoom(gesture.camera,mid(gesture.points),distance(points)/Math.max(1,distance(gesture.points)));const a=mid(gesture.points),b=mid(points);camera.x+=b.x-a.x;camera.y+=b.y-a.y;paintCamera();}});
  for(const name of ['pointerup','pointercancel'])listen(viewport,name,event=>{pointers.delete(event.pointerId);gesture=pointers.size?{camera:{...camera},points:[...pointers.values()]}:null;});
  listen(viewport,'keydown',event=>{const moves={ArrowLeft:[30,0],ArrowRight:[-30,0],ArrowUp:[0,30],ArrowDown:[0,-30]};if(moves[event.key]){event.preventDefault();camera.x+=moves[event.key][0];camera.y+=moves[event.key][1];paintCamera();}else if(['+','=','-'].includes(event.key)){event.preventDefault();zoom({x:600,y:350},event.key==='-'?1/1.2:1.2);}});
  listen(preference,'change',async()=>{const ticket=++preferenceSerial,value=preference.checked;preference.disabled=true;
    try{const saved=await writePreference?.(value);if(!current()||ticket!==preferenceSerial)return;preference.checked=saved===true;preferenceStatus.textContent=saved===true?'Verified current source may be added on qualifying turns.':'Automatic context is off.';}
    catch{if(current()&&ticket===preferenceSerial){preference.checked=false;preferenceStatus.textContent='Setting unavailable; automatic context is off.';}}
    finally{if(current()&&ticket===preferenceSerial)preference.disabled=false;}});
  const prefTicket=++preferenceSerial;
  Promise.resolve().then(()=>readPreference?.()).then(value=>{if(current()&&prefTicket===preferenceSerial){preference.checked=value===true;preference.disabled=!writePreference;preferenceStatus.textContent=value===true?'Automatic context enabled.':'Automatic context is off.';}},()=>{if(current()&&prefTicket===preferenceSerial)preferenceStatus.textContent='Setting unavailable; automatic context is off.';});
  function menuCapture(){return {workspace,selected,indexSerial,topologySerial,receipt:codeReceiptKey(data.receipt),operation:data.operation||null,status:data.status||null,actionSerial,search:search.value,searchMode:searchMode.value,pathPrefix:pathPrefix.value,symbolKinds:symbolKinds.value,filters:JSON.stringify(filters()),traceTarget:traceTarget.value};}
  function menuIsCurrent(c){return current()&&c&&JSON.stringify(c)===JSON.stringify(menuCapture());}
  function menuCommands(c){if(!menuIsCurrent(c))return [];
    const controls=[['overview','Evidence overview','View',overview],['files','Files','View',catalog],['symbols','Symbols','View',symbolTab],['filters','Folders & filters','View',folderFacets],
      ['topology-capture','Capture selected indexed topology','File',captureTopology],['topology-status','Check original topology capture','File',checkTopology],['topology-compare','Compare saved topology','Graph',compareTopology],['import-analysis','Analyze import cycles & hubs','Graph',importAnalysis],['library-api','Inspect on-disk package API','Graph',libraryLookup],['download-fragment','Download evidence fragment','File',downloadFragment],['import-fragment','Import evidence fragment','File',importFragment],['focus','Focus selected evidence','Graph',focus],['reset','Reset evidence camera','View',reset],['zoom-in','Zoom evidence in','View',zoomIn],['zoom-out','Zoom evidence out','View',zoomOut],
      ...evidenceButtons.map((control,i)=>[['open','add','ask','summary','history','compare'][i],['Open selected source','Add selected evidence to chat','Ask about selected evidence','Summarize selected evidence','Recorded changes','Compare local dependency'][i],i===0||i===4?'File':'Graph',control]),
      ...operationButtons.filter(([control,,requires])=>requires).map(([control,operation])=>[operation+'-'+control.textContent.toLowerCase().replace(/[^a-z0-9]+/g,'-'),control.textContent,'Graph',control])];
    return controls.map(([id,label,menu,control])=>({id:'graph.evidence-'+id,label,menu,disabledReason:control.disabled||id==='focus'&&!selected?'Unavailable for the current evidence.':'',run:()=>control.click()}));
  }
  restoreTopology(workspace);install();paintIndex();if(workspace)startIndex();
  function dispose(){if(disposed)return;disposed=true;comparisonOffer=null;pendingComparisonNavigation=null;fragmentSerial++;recordedFragment=null;indexSerial++;indexRelease?.();indexRelease=null;actionSerial++;preferenceSerial++;capabilitySerial++;resourceAborter.abort();semanticAborter?.abort();resetVectorIndex();aborter.abort();model?.dispose();pointers.clear();observer.disconnect();}
  return {applyQualifiedImpact,inspectResource,configureSettings(){actionSerial++;resourceAborter.abort();resourceAborter=new AbortController();model.dispose();selected=null;query=null;windowStart=0;positions.clear();install();},run,selectWorkspace,isCurrent:current,isMounted:()=>!disposed&&element.isConnected,
    snapshot:()=>model.snapshot(),camera:()=>({...camera}),presentationStats:()=>({positionCount:positions.size}),menuCapture,menuIsCurrent,menuCommands,dispose};
}
