// Disposable presentation over C04. No source/index/authority persistence.
import { createCodeGraphModel, codeReceiptKey, codeFacetCount } from './codeGraphModel.js';
import { indexPresentation } from './codeIndexObserver.js';
import { mountCodeMapWorkbench } from './codeMapWorkbench.js';

export const WORKBENCH_LIMITS = Object.freeze({ mounted:200, loaded:1000, mountedEdges:400, loadedEdges:1000,
  loadedPaths:100, loadedFacetBuckets:1000, inspectorWitnesses:50, breadcrumbPaths:10, breadcrumbNodes:80 });
export function codeCanContinue(data) {
  return Boolean(data.status==='ready' && data.next_cursor && codeFacetCount(data.facets)<WORKBENCH_LIMITS.loadedFacetBuckets
    && data.nodes.length<WORKBENCH_LIMITS.loaded && data.edges.length<WORKBENCH_LIMITS.loadedEdges && data.paths.length<WORKBENCH_LIMITS.loadedPaths);
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
  let disposed=false, workspace=workspaceId, model, data={nodes:[],edges:[],paths:[]}, selected=null;
  let camera={x:0,y:0,scale:1}, positions=new Map(), actionSerial=0, query=null, preferenceSerial=0, windowStart=0;
  let resourceCapability={available:false,reason:'Current source bridge not verified.'},capabilitySerial=0;
  let layoutReceipt=null;
  let resourceAborter=new AbortController();
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
  const searchButton=button('Search',()=>run('search'));
  const overview=button('Overview',()=>run('overview'),{'data-code-operation':'overview'});
  const catalog=button('Files',()=>run('catalog'),{'data-code-operation':'catalog'});
  const folderFacets=button('Folders & filters',()=>run('facets'),{'data-code-operation':'facets'});
  const symbolTab=button('Symbols',()=>run('search',{query:search.value || undefined,filter:{...filters(),kinds:['declaration']}}),{'data-code-operation':'search'});
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
  const facets=Object.fromEntries(['folders','languages','states','kinds'].map(key=>[key,el('select',{'aria-label':`Code ${key}`},el('option',{value:'',text:`All ${key}`}))]));
  const list=el('div',{class:'code-workbench-list',role:'listbox','aria-label':'Code resources','data-code-list':true});
  const more=button('Load more',()=>run(null,{more:true}),{disabled:true});
  const previousWindow=button('Previous resources',()=>{windowStart=Math.max(0,windowStart-WORKBENCH_LIMITS.mounted);show(data);});
  const nextWindow=button('Next resources',()=>{if(windowStart+WORKBENCH_LIMITS.mounted<data.nodes.length){windowStart+=WORKBENCH_LIMITS.mounted;show(data);}});
  const windowLabel=el('p',{'data-code-window':true});
  const explorer=el('aside',{class:'code-workbench-explorer','aria-label':'Code explorer'},
    el('h3',{text:'Explore'}),el('div',{class:'code-workbench-search'},search,searchButton),
    el('div',{class:'code-workbench-tabs',role:'group','aria-label':'Code scope'},overview,catalog,symbolTab,folderFacets),
    ...Object.values(facets),list,el('div',{class:'code-workbench-tabs'},previousWindow,nextWindow),windowLabel,more);
  const viewport=el('div',{class:'code-workbench-viewport','data-code-viewport':true,tabindex:0,role:'region','aria-label':'Code graph. Arrow keys pan; plus and minus zoom.'});
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1200 700');svg.setAttribute('aria-hidden','true');
  const defs=document.createElementNS(svg.namespaceURI,'defs'),marker=document.createElementNS(svg.namespaceURI,'marker');
  const markerId=`code-arrow-${crypto.randomUUID()}`;for(const [key,value]of Object.entries({id:markerId,viewBox:'0 0 10 10',refX:'18',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto-start-reverse'}))marker.setAttribute(key,value);
  const arrow=document.createElementNS(svg.namespaceURI,'path');arrow.setAttribute('d','M 0 0 L 10 5 L 0 10 z');arrow.setAttribute('fill','context-stroke');marker.append(arrow);defs.append(marker);svg.append(defs);
  const world=document.createElementNS(svg.namespaceURI,'g');svg.append(world);viewport.append(svg);
  const legend=el('p',{class:'code-workbench-legend',text:'● File   ◆ Declaration   ○ Occurrence · solid: exact static binding · dashed: inferred/unresolved. Not runtime execution.'});
  const breadcrumbs=el('nav',{'aria-label':'Ordered static path',class:'code-path-breadcrumbs'});
  const reset=button('Reset camera',()=>{camera={x:0,y:0,scale:1};paintCamera();});
  const zoomIn=button('Zoom in',()=>zoom({x:600,y:350},1.2));
  const zoomOut=button('Zoom out',()=>zoom({x:600,y:350},1/1.2));
  const center=el('section',{class:'code-workbench-center'},el('div',{class:'code-camera-controls'},zoomIn,zoomOut,reset),viewport,legend,breadcrumbs);
  const title=el('h3',{text:'Select a resource'}), fact=el('p',{'data-code-fact':true}), source=el('p',{'data-code-source':true});
  const detail=el('pre',{'data-code-detail':true,class:'code-workbench-detail'});
  const witnesses=el('div',{'data-code-witnesses':true});
  const actions=el('div',{class:'code-workbench-actions'});
  const operationButtons=[];
  const addOp=(label,operation,extra={})=>{
    const control=button(label,()=>{if(selected)run(operation,{seed_ids:[selected],...extra});},{'data-code-operation':operation});
    operationButtons.push([control,operation,true]);actions.append(control);return control;
  };
  addOp('Declaration outline','outline');addOp('Incoming','neighbors',{direction:'incoming'});
  addOp('Outgoing','neighbors',{direction:'outgoing'});addOp('Impact','impact',{direction:'incoming'});
  const traceTarget=el('select',{'aria-label':'Static path target'},el('option',{value:'',text:'Choose path target…'}));
  const path=button('Trace to',()=>{if(selected && traceTarget.value)run('path',{seed_ids:[selected],target_id:traceTarget.value,direction:'outgoing'});},{'data-code-operation':'path'});
  operationButtons.push([path,'path',true]);actions.append(traceTarget,path);
  const focus=button('Focus selected',()=>{if(selected)run('neighbors',{seed_ids:[selected],direction:'both'});});actions.append(focus);
  const evidenceButtons=[];
  for(const [kind,label] of [['open','Open source'],['add','Add to chat'],['ask','Ask about this']]){
    const control=button(label,async()=>{
      if(!selected || !current())return;
      const node=data.nodes.find(row=>row.id===selected), receipt=structuredClone(data.receipt), ticket=++actionSerial;
      const key=codeReceiptKey(receipt), id=selected, scope=workspace;
      const valid=()=>current() && ticket===actionSerial && selected===id && workspace===scope && codeReceiptKey(data.receipt)===key;
      control.disabled=true;
      try {const result=await onEvidenceAction?.(kind,{node,receipt,workspaceId:scope,isCurrent:valid,signal:resourceAborter.signal});if(valid())status.textContent=result?.message || (result?.ok?'Source action completed.':'Current source action unavailable.');}
      catch {if(valid())status.textContent='Current source action unavailable.';}
      finally{if(valid())updateActions();}
    });evidenceButtons.push(control);actions.append(control);
  }
  const inspector=el('aside',{class:'code-workbench-inspector','aria-label':'Selected code evidence'},title,fact,source,actions,witnesses,detail);
  const diagnostics=el('details',{class:'code-workbench-diagnostics'},el('summary',{text:'Coverage, channels and omissions'}),el('pre',{'data-code-facets':true}));
  element.append(header,indexStatus,indexControls,status,counts,el('div',{class:'code-workbench-panels'},explorer,center,inspector),diagnostics);
  operationButtons.push([overview,'overview',false],[catalog,'catalog',false],[symbolTab,'search',false],[folderFacets,'facets',false],[searchButton,'search',false]);
  function filters(){return {path_prefix:facets.folders.value?`${facets.folders.value}/`:undefined,
    ...Object.fromEntries(['languages','states','kinds'].map(key=>[key,facets[key].value?[facets[key].value]:[]]))};}
  function install(){model=createCodeGraphModel({request,isCurrent:current,changed:show});onController(model);show(model.snapshot());}
  async function loadResourceCapabilities(){const ticket=++capabilitySerial,id=workspace;resourceCapability={available:false,reason:'Verifying current source capability.'};
    try{const result=await readResourceCapabilities?.(id,resourceAborter.signal);if(current()&&ticket===capabilitySerial&&id===workspace)resourceCapability=result?.available===true?result:{available:false,reason:result?.reason||'Source bridge unavailable.'};}
    catch{if(current()&&ticket===capabilitySerial)resourceCapability={available:false,reason:'Source bridge unavailable.'};}
    if(current()&&ticket===capabilitySerial)updateActions();
  }
  function paintIndex(){
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
    actionSerial++;capabilitySerial++;resourceAborter.abort();resourceAborter=new AbortController();selected=null;model?.dispose();install();query=null;
    title.textContent='Select a resource';fact.textContent='';source.textContent='';detail.textContent='';witnesses.replaceChildren();
    indexState=indexPresentation({lifecycle:'starting'});paintIndex();
    if(!subscribeIndex){indexState=indexPresentation({lifecycle:'unavailable'});paintIndex();return;}
    let loadedHead=null;
    const subscription=subscribeIndex(id,{isCurrent:()=>current()&&ticket===indexSerial&&workspace===id,onStatus:index=>{
      if(!current()||ticket!==indexSerial||workspace!==id)return;
      indexState=indexPresentation(index);paintIndex();
      if(!indexState.usable){loadedHead=null;actionSerial++;capabilitySerial++;resourceAborter.abort();resourceAborter=new AbortController();selected=null;model?.dispose();install();}
      if(indexState.usable&&loadedHead!==index.head_generation){loadedHead=index.head_generation;void loadResourceCapabilities();if(initialQuery)run('overview');}
    }});
    if(!current()||ticket!==indexSerial)subscription?.();else indexRelease=subscription;
    paintIndex();
  }
  function selectWorkspace(id,{label:nextLabel=id}={}){if(!current())return;folderSerial++;actionSerial++;if(workspace!==id)for(const control of Object.values(facets))control.value='';workspace=id;label=nextLabel;setWorkspace(id,label);scope.textContent=label;
    selected=null;windowStart=0;layoutReceipt=null;camera={x:0,y:0,scale:1};positions.clear();resourceCapability={available:false,reason:'Waiting for an indexed head.'};startIndex();}
  function run(operation,extra={}){
    if(!current() || !workspace || !indexState.usable)return false;
    actionSerial++;
    if(extra.more)return query && model.run(workspace,query,{more:true});
    windowStart=0;
    query={schema:'open-clank.code-query/v1',operation:operation||'overview',query:search.value||undefined,fields,
      filter:filters(),budgets:{rows:50,bytes:65536,tokens:8192,hops:4,work:4000000},
      ...(operation==='search'?{channels:['exact','identifier','fuzzy']}:{}),...extra};
    return model.run(workspace,query);
  }
  function updateActions(){
    for(const [control,operation,requiresSelection] of operationButtons){const cap=capabilityFor(data,operation);
      control.disabled=!workspace || !indexState.usable || data.status==='loading' || (!cap.available && data.status==='ready') || (requiresSelection&&!selected);
      control.title=cap.available?'':cap.reason;
    }
    for(const control of Object.values(facets))control.disabled=!indexState.usable||data.status==='loading';
    search.disabled=!indexState.usable;
    focus.disabled=!indexState.usable || !selected || !capabilityFor(data,'neighbors').available;
    // These actions use the separately verified source bridge, not native raw
    // reads (deliberately unavailable in the metadata engine: Files owns I/O).
    for(const control of evidenceButtons){const typed=/^(file|decl|occ):[0-9a-f]{64}$/i.test(selected||'');control.disabled=!typed||!onEvidenceAction||data.status!=='ready'||!resourceCapability.available;
      control.disabled ||= !indexState.usable;
      control.title=!typed?'A canonical indexed resource identity is required.':!resourceCapability.available?resourceCapability.reason:'Revalidate current authorized source before action.';}
  }
  function select(id,{focusList=false}={}){
    if(!current() || !data.nodes.some(row=>row.id===id))return;
    actionSerial++;selected=id;const node=data.nodes.find(row=>row.id===id);
    title.textContent=node.label||node.id;
    fact.textContent=`${node.evidence_state||'unknown'} · ${node.kind} · ${node.current_source_verified===true?'bounded current-source observation':'metadata; current bytes not verified'}`;
    source.textContent=node.source?`${node.source.rel_path} · lines ${node.source.span?.start_line??'?'}–${node.source.span?.end_line??'?'} · hash ${node.source.source_hash||'unknown'}`:'Source locator omitted or unavailable.';
    detail.textContent=JSON.stringify(node,null,2);detail.dataset.receipt=JSON.stringify(data.receipt);
    const edges=data.edges.filter(edge=>edge.from===id||edge.to===id);
    witnesses.replaceChildren(el('h4',{text:`Incoming / outgoing witnesses (${edges.length} loaded; ${Math.min(edges.length,WORKBENCH_LIMITS.inspectorWitnesses)} shown)`}),
      ...edges.slice(0,WORKBENCH_LIMITS.inspectorWitnesses).map(edge=>el('details',{},el('summary',{text:`${edge.to===id?'Incoming':'Outgoing'} · ${edge.resolution_state} · ${edge.occurrence_id}`}),
        el('pre',{text:JSON.stringify(edge,null,2)}))),
      ...(edges.length>WORKBENCH_LIMITS.inspectorWitnesses?[el('p',{text:`${edges.length-WORKBENCH_LIMITS.inspectorWitnesses} loaded witnesses omitted from inspector.`})]:[]));
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
    if(data.status==='ready'&&data.receipt){const key=codeReceiptKey(data.receipt);if(layoutReceipt&&layoutReceipt!==key){positions.clear();camera={x:0,y:0,scale:1};selected=null;windowStart=0;}layoutReceipt=key;}
    if(data.status==='ready'){const retained=new Set(data.nodes.map(node=>node.id));for(const id of positions.keys())if(!retained.has(id))positions.delete(id);}
    const facetBuckets=codeFacetCount(data.facets),facetLimit=facetBuckets>=WORKBENCH_LIMITS.loadedFacetBuckets;
    status.textContent=data.error?`Code query ${data.error.code}: ${data.error.message || 'Recheck index status or choose an authorized folder.'}`:`${data.operation==='facets'?`${data.status} · ${facetBuckets} filter options loaded${data.next_cursor?(facetLimit?' · option limit reached; more remain':' · more filter options available'):data.status==='ready'?' · filter option page stream complete':''}`:data.status==='ready'&&!data.nodes.length?'Ready · no indexed code resources':data.status}${data.partial?' · partial':''}${data.degraded?' · degraded':''}${data.reasons?.length?' · '+data.reasons.join(', '):''} · metadata is not verified-current source`;
    windowStart=Math.min(windowStart,Math.max(0,Math.floor((data.nodes.length-1)/WORKBENCH_LIMITS.mounted)*WORKBENCH_LIMITS.mounted));
    const nodes=data.nodes.slice(windowStart,windowStart+WORKBENCH_LIMITS.mounted);
    previousWindow.disabled=windowStart===0;nextWindow.disabled=windowStart+nodes.length>=data.nodes.length;
    windowLabel.textContent=`Resources ${nodes.length?windowStart+1:0}–${windowStart+nodes.length} of ${data.nodes.length} loaded; backend total is reported separately.`;
    const ids=new Set(nodes.map(node=>node.id)),visibleEdges=data.edges.filter(edge=>ids.has(edge.from)&&ids.has(edge.to)),mountedEdges=visibleEdges.slice(0,WORKBENCH_LIMITS.mountedEdges);
    counts.textContent=`Returned this page: ${data.returned?.hits||0} hits, ${data.returned?.edges||0} edges, ${data.returned?.paths||0} paths; unique loaded: ${data.nodes.length}; mounted: ${nodes.length} (viewport cap 200, loaded cap 1000). Edges loaded: ${data.edges.length}, mounted: ${mountedEdges.length}, omitted from viewport: ${data.edges.length-mountedEdges.length}; paths loaded: ${data.paths.length}. Continuation stops at 1000 nodes / 1000 edges / 100 paths; whole final page retained (up to 50-row overshoot). `+(data.counts||[]).map(row=>`${row.name}: ${row.exactness==='exact'&&row.value!=null?row.value:'?'} ${row.unit}`).join('; ');
    list.replaceChildren(...nodes.map((node,index)=>button(`${node.label||node.id} · ${node.kind} · ${node.evidence_state||'unknown'}`,()=>select(node.id),{'data-code-id':node.id,role:'option','aria-selected':node.id===selected,tabindex:index===0?0:-1})));
    renderGraph(nodes,mountedEdges);
    const oldTarget=traceTarget.value;traceTarget.replaceChildren(el('option',{value:'',text:'Choose path target…'}),...data.nodes.map(node=>el('option',{value:node.id,text:node.label||node.id})));traceTarget.value=oldTarget;
    if(selected&&!data.nodes.some(node=>node.id===selected)){selected=null;title.textContent='Selection outside this result';fact.textContent='Select a visible resource.';source.textContent='';detail.textContent='';witnesses.replaceChildren();}
    if(selected)select(selected);
    if(data.paths?.length){const paths=data.paths.slice(0,WORKBENCH_LIMITS.breadcrumbPaths),allIds=paths.flatMap(path=>path.node_ids),omittedPaths=data.paths.length-paths.length,omittedNodes=Math.max(0,allIds.length-WORKBENCH_LIMITS.breadcrumbNodes);
      detail.textContent=JSON.stringify({ordered_paths:paths,omitted_paths:omittedPaths},null,2);breadcrumbs.replaceChildren(...allIds.slice(0,WORKBENCH_LIMITS.breadcrumbNodes).map(id=>button(data.nodes.find(node=>node.id===id)?.label||id,()=>select(id))),
        el('span',{text:`${omittedPaths} loaded paths / ${omittedNodes} breadcrumb nodes omitted.`}));}
    else breadcrumbs.replaceChildren();
    if(data.raw_windows?.length||data.source_window)detail.textContent=JSON.stringify({state:'explicit_noncanonical_window',source_window:data.source_window,windows:data.raw_windows},null,2);
    detail.dataset.receipt=JSON.stringify(data.receipt);
    counts.textContent+=` Filter options returned this page: ${data.facet_returned||0}; unique loaded: ${facetBuckets}; continuation limit ${WORKBENCH_LIMITS.loadedFacetBuckets} buckets across axes (whole final page retained).${data.next_cursor&&facetLimit?' More options remain beyond this presentation limit.':data.next_cursor&&data.nodes.length>=WORKBENCH_LIMITS.loaded?' Use Folders & filters for independent option pages.':''}`;
    more.disabled=!codeCanContinue(data);
    for(const [key,control]of Object.entries(facets)){const value=control.value,rows=data.facets?.[key]||[];control.replaceChildren(el('option',{value:'',text:`All ${key}`}),...rows.map(row=>el('option',{value:row.value,text:`${row.value||'(root)'} · ${row.exactness==='exact'?row.count:'?'} ${row.unit}`})));if(value&&!rows.some(row=>row.value===value))control.append(el('option',{value,text:`${value} · outside result`}));control.value=value;}
    diagnostics.querySelector('pre').textContent=JSON.stringify({facets:data.facets,omissions:data.omissions,channels:data.channels,capabilities:data.capabilities},null,2);updateActions();
  }
  for(const control of Object.values(facets))listen(control,'change',()=>run('catalog'));
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
  install();paintIndex();if(workspace)startIndex();
  function dispose(){if(disposed)return;disposed=true;indexSerial++;indexRelease?.();indexRelease=null;actionSerial++;preferenceSerial++;capabilitySerial++;resourceAborter.abort();aborter.abort();model?.dispose();pointers.clear();observer.disconnect();}
  return {run,selectWorkspace,isCurrent:current,isMounted:()=>!disposed&&element.isConnected,
    snapshot:()=>model.snapshot(),camera:()=>({...camera}),presentationStats:()=>({positionCount:positions.size}),dispose};
}
