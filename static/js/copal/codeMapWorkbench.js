// Compact app-only geometry with the existing evidence workbench on demand.
// One index subscriber owns admission/observation; visual reads never index.
import { MAP_SCHEMA, codeMapKey, createCodeMapModel } from './codeMapModel.js';
import { createCodeMapScene } from './codeMapScene.js';
import { indexPresentation } from './codeIndexObserver.js';
import { resolveCodeMapPath } from './codeMapPath.js';

const copy=value=>structuredClone(value);
export function codeMapThemePalette(style) {
  const hex=value=>{const text=String(value||'').trim();if(/^#[\da-f]{6}$/i.test(text))return text;
    if(/^#[\da-f]{3}$/i.test(text))return '#'+[...text.slice(1)].map(c=>c+c).join('');
    const rgb=/^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$/.exec(text);
    return rgb&&rgb.slice(1).every(n=>Number(n)<=255)?'#'+rgb.slice(1).map(n=>Number(n).toString(16).padStart(2,'0')).join(''):null;};
  const choices={background:['--panel','--bg'],node:['--fg'],group:['--accent'],selected:['--accent-focus','--accent'],label:['--fg'],containment:['--border'],exact:['--color-accent','--accent'],inferred:['--color-warning','--fg'],import:['--color-success','--accent']};
  return Object.fromEntries(Object.entries(choices).flatMap(([key,names])=>{const color=names.map(n=>hex(style.getPropertyValue(n))).find(Boolean);return color?[[key,color]]:[];}));
}
export function codeMapIndexKey(index={}) {
  return codeMapKey(Object.fromEntries(['registration_id','admission_epoch','policy_generation','head_generation'].map(k=>[k,index[k]??null])));
}
export function codeMapMayRead(index={}) {
  return ['admitted','queued','building','running','ready','failed','cancelled'].includes(index.lifecycle)
    && (index.lifecycle!=='failed'||Boolean(index.head_generation));
}
// Diagnostic presentation is not the index API. Never copy every source path,
// parser error or scene label into a hidden DOM node on each progress update.
export function codeMapIndexDiagnostics(index={}) {
  const out={},scalar=value=>typeof value==='string'?value.slice(0,256):typeof value==='boolean'||Number.isFinite(value)?value:undefined;
  for(const key of ['workspace_id','workspace_revision','registration_id','checkout_id','admission_epoch','policy_generation','run_id','head_generation','lifecycle','freshness','config_fingerprint','recovering','coverage_complete']){
    const value=scalar(index[key]);if(value!==undefined)out[key]=value;
  }
  out.progress={};for(const key of ['completed_files','total_files','heartbeat_at']){const value=scalar(index.progress?.[key]);if(value!==undefined)out.progress[key]=value;}
  out.error=index.error?{code:scalar(index.error.code),message:typeof index.error.message==='string'?index.error.message.slice(0,512):undefined}:null;
  out.errorCount=Array.isArray(index.errors)?index.errors.length:null;
  out.coverageSummary=null;
  if(Array.isArray(index.coverage)){
    const known=new Set(['selected','indexed','no_source_units','no_units','parse_failed','binary','ignored','outside_root','oversize','oversized','unsupported','unavailable','permission_denied']),statuses={};
    for(const row of index.coverage){const key=known.has(row?.status)?row.status:'other';statuses[key]=(statuses[key]||0)+1;}
    out.coverageSummary={entries:index.coverage.length,statuses};
  }
  return out;
}
// Retained geometry is a cache, not the visible scene. Drilldown renders one
// level (or the selected adjacency), plus its breadcrumb ancestors.
export function codeMapVisible(data={},query={}) {
  const nodes=data.nodes||[],edges=data.edges||[],byId=new Map(nodes.map(n=>[n.id,n]));
  const root=data.root||nodes.find(n=>n.kind==='repository')?.id||null,ids=new Set(root?[root]:[]);
  if(query?.operation==='neighbors'){
    for(const edge of edges)if(edge.from===root||edge.to===root){ids.add(edge.from);ids.add(edge.to);}
  }else{
    for(const node of nodes)if(node.parent===root||node.kind==='repository'&&!root)ids.add(node.id);
  }
  let parent=byId.get(root)?.parent;const seen=new Set();
  while(parent&&byId.has(parent)&&!seen.has(parent)&&seen.size<64){seen.add(parent);ids.add(parent);parent=byId.get(parent).parent;}
  return {...data,nodes:nodes.filter(n=>ids.has(n.id)),edges:edges.filter(e=>ids.has(e.from)&&ids.has(e.to))};
}
export function codeMapStatus(index={},map={}) {
  const lifecycle=index.lifecycle||'missing',pending=indexPresentation(index).pending;
  const codes={denied:'Access denied. Choose an authorized folder.',erased:'This index was erased. Readmit it explicitly to start again.',stale_cursor:'The code index changed. Reload the map.',reindex_required:'This older index needs an explicit rebuild.',budget_exceeded:'This map exceeds the current reading budget. Choose a smaller code folder.',busy:'The index is busy. Retry the map shortly.',cancelled:'Map loading was cancelled.'};
  if(['denied','erased'].includes(lifecycle))return codes[lifecycle];
  if(map.status==='error')return codes[map.error?.code]||'Code map unavailable. Retry or inspect details.';
  if(lifecycle==='unavailable')return index.recovering?'Index connection interrupted; rechecking status.':'Index connection unavailable. Recheck status.';
  if(map.authority?.variant==='structure_preview')return `Folder structure · ${pending?'indexing dependencies':'dependencies not indexed'}${map.coverage?.inventory_complete?'':' · partial inventory'}`;
  if(map.status==='ready') {
    if(!map.nodes?.length)return 'No visible code or folders in this scope.';
    if(pending){const p=index.progress||{},done=Number(p.completed_files),total=Number(p.total_files);return `Previous index · checking files${Number.isFinite(done)&&Number.isFinite(total)&&total>0?` · ${done}/${total}`:''}`;}
    if(lifecycle==='cancelled')return 'Previous index · verification cancelled. Retry indexing to continue.';
    if(lifecycle==='failed')return 'Previous index · rebuild failed. Source actions revalidate current files.';
    return map.coverage?.dependencies==='partial'?'Code map · partial dependency coverage':'Code map ready';
  }
  if(pending){const p=index.progress||{},done=Number(p.completed_files),total=Number(p.total_files);return `Indexing dependencies${Number.isFinite(done)&&Number.isFinite(total)&&total>0?` · ${done}/${total} files`:''} · loading folder structure`;} 
  return ({cancelled:'Indexing cancelled. Retry explicitly to continue.',failed:'Indexing failed. View details or retry.',missing:'Choose a code folder to explore.'})[lifecycle]||'Loading code map…';
}
const el=(tag,attrs={},...children)=>{
  const node=document.createElement(tag);
  for(const [key,value]of Object.entries(attrs))if(key==='text')node.textContent=value;else if(key==='class')node.className=value;else if(value!=null&&value!==false)node.setAttribute(key,value===true?'':String(value));
  node.append(...children);return node;
};

export function mountCodeMapWorkbench(options) {
  const {element,isCurrent=()=>true,workspaceId='',workspaceLabel='',setWorkspace=()=>{},chooseWorkspace,subscribeIndex,
    requestMap,request,onEvidenceAction,readResourceCapabilities,readPreference,writePreference,mountEvidence,
    sceneFactory=createCodeMapScene}=options;
  let disposed=false,workspace=workspaceId,label=workspaceLabel,epoch=0,indexSerial=0,folderSerial=0,actionSerial=0,preferenceSerial=0;
  let index={lifecycle:'missing'},release=null,loadedKey=null,mapData={},selected=null,query=null,view='map',listPage=0,hostVisible=true;
  let legacy=null,scene=null,sceneKey=null,sceneAuthorityKey=null,sceneQuery=null,rendererDiagnostic=null,capability={available:false},capabilityKey=null,capabilityPending=null;
  let actionAborter=new AbortController(),capabilityAborter=new AbortController();
  let focusPath=options.focusPath||'',focusKey=null,focusSerial=0,focusMessage='';
  const subscribers=new Set(),aborter=new AbortController(),signal=aborter.signal;
  const current=()=>!disposed&&isCurrent()&&element.isConnected;
  const listen=(node,name,fn,opts={})=>node.addEventListener(name,fn,{...opts,signal});
  const button=(text,fn,attrs={})=>{const b=el('button',{type:'button',class:'copal-btn',text,...attrs});listen(b,'click',fn);return b;};
  const identity=()=>({workspace,epoch});
  element.classList.add('code-map-workbench');element.dataset.codeGraph='true';
  const workspaceName=el('strong',{'data-code-workspace':true,text:label||workspace||'Code Graph'});
  const folder=button('Choose code folder',async()=>{
    const ticket=++folderSerial;
    try{const binding=await chooseWorkspace?.();if(current()&&ticket===folderSerial&&binding?.id)selectWorkspace(binding.id,{label:binding.name||binding.path||binding.id});}
    catch{if(current()&&ticket===folderSerial)status.textContent='Code folder unavailable. Choose an authorized folder.';}
  });
  const search=el('input',{type:'search',maxlength:1024,'aria-label':'Search scoped code',placeholder:'Search code…'});
  const searchButton=button('Search',()=>evidence('search',{query:search.value||undefined}));
  const overview=button('Overview',()=>{stopRequestedFocus();showView('map');return loadMap('overview');},{'data-code-operation':'overview'});
  const exploreToggle=button('Explore',()=>togglePanel('explorer'),{'aria-expanded':'false','aria-controls':'code-map-explorer-'+crypto.randomUUID()});
  const inspectToggle=button('Inspector',()=>togglePanel('inspector'),{'aria-expanded':'false','aria-controls':'code-map-inspector-'+crypto.randomUUID()});
  const mapButton=button('Map',()=>showView('map'),{'aria-pressed':'true'});
  const evidenceButton=button('Search & evidence',()=>{showView('evidence');if(indexPresentation(index).usable&&!legacy?.snapshot().nodes.length)evidence('catalog');},{'aria-pressed':'false'});
  const status=el('span',{'data-code-index-status':true,role:'status','aria-live':'polite'});
  const rendererStatus=el('span',{'data-code-renderer':true,text:'Preparing renderer'});
  const details=el('details',{class:'code-map-menu'},el('summary',{text:'Menu','aria-label':'Code map menu'}));
  const recheck=button('Recheck status',()=>release?.refresh?.());
  const retry=button('Retry indexing',()=>startIndex());
  const readmit=button('Readmit erased index',()=>{if(index.lifecycle==='erased')void release?.readmit?.();});
  const detach=button('Stop observing',()=>{indexSerial++;release?.();release=null;invalidate();index={lifecycle:'unavailable'};notifyEvidence();paint();});
  const preference=el('input',{type:'checkbox','aria-label':'Automatic code context',disabled:true});
  const preferenceText=el('span',{text:'Automatic context is off until verified.'});
  const diagnostics=el('pre',{'data-code-diagnostics':true});
  details.append(el('div',{class:'code-map-menu-content'},folder,el('div',{class:'code-map-search'},search,searchButton),
    el('div',{class:'code-map-toolbar'},mapButton,evidenceButton,overview),
    el('div',{class:'code-map-toolbar'},button('Zoom in',()=>scene?.zoom(1.2)),button('Zoom out',()=>scene?.zoom(1/1.2))),
    el('details',{},el('summary',{text:'Index & settings'}),el('div',{class:'code-map-menu-settings'},recheck,retry,readmit,detach,
      el('label',{},preference,' Automatic code context'),preferenceText,
      el('details',{},el('summary',{text:'Coverage and diagnostics'}),diagnostics)))));
  const header=el('header',{class:'code-map-header'},workspaceName,details);
  const toolbar=el('nav',{class:'code-map-toolbar code-map-primary-tools','aria-label':'Code map navigation'},exploreToggle,inspectToggle,
    button('Fit',()=>{scene?.fit();revealSelection();}));
  const viewport=el('div',{class:'code-map-viewport','data-code-viewport':true});
  const empty=el('div',{class:'code-map-empty','data-code-empty':true});
  const mapReload=button('Reload map',()=>{stopRequestedFocus();return loadMap('overview');});
  empty.append(el('span',{text:'Choose a code folder to see its structure.'}),mapReload);
  const list=el('div',{class:'code-map-list',role:'listbox','aria-label':'Code map resources'});
  const listLabel=el('span',{'data-code-map-list-count':true});
  const previous=button('Previous',()=>{listPage=Math.max(0,listPage-1);paintList();});
  const next=button('Next',()=>{listPage++;paintList();});
  const more=button('Load more',()=>{stopRequestedFocus();return query&&loadMap(query.operation,{more:true});});
  const explorer=el('aside',{id:exploreToggle.getAttribute('aria-controls'),class:'code-map-explorer','aria-label':'Code map explorer',hidden:true},
    el('div',{class:'code-map-panel-heading'},el('strong',{text:'Folders & files'}),button('Close explorer',()=>togglePanel('explorer',false))),
    list,listLabel,el('div',{class:'code-map-toolbar'},previous,next),more);
  const inspectorTitle=el('strong',{text:'Select a folder, file or relationship'}),inspectorSummary=el('p');
  const drill=button('Open folder',()=>{stopRequestedFocus();return selected&&loadMap('children',{root:{kind:'visual',id:selected}});});
  const neighbors=button('Connections',()=>{stopRequestedFocus();return selected&&loadMap('neighbors',{root:{kind:'visual',id:selected}});});
  const inspectEvidence=button('Inspect evidence',()=>mapData.selection&&evidence('outline',{seed_ids:[mapData.selection.resource_id]}));
  const evidenceActions=['open','add','ask'].map((kind,n)=>button(['Open source','Add to chat','Ask about this'][n],()=>act(kind)));
  const edgeList=el('div',{class:'code-map-relationships'}),referenceList=el('div',{class:'code-map-references'});
  const inspector=el('aside',{id:inspectToggle.getAttribute('aria-controls'),class:'code-map-inspector','aria-label':'Selected code map item',hidden:true},
    el('div',{class:'code-map-panel-heading'},inspectorTitle,button('Close inspector',()=>togglePanel('inspector',false))),inspectorSummary,
    el('div',{class:'code-map-toolbar'},drill,neighbors,inspectEvidence,...evidenceActions),edgeList,referenceList);
  const mapPanel=el('section',{class:'code-map-panel','aria-label':'Code map'},viewport,empty,explorer,inspector);
  const breadcrumbs=el('nav',{class:'code-map-breadcrumbs','aria-label':'Code folder path'});mapPanel.append(breadcrumbs);
  const evidencePanel=el('section',{class:'code-map-evidence','aria-label':'Code search and detailed evidence',hidden:true});
  const legend=el('span',{text:'Folders and files · containment / imports / calls · static evidence, not execution'});
  element.append(header,toolbar,el('div',{class:'code-map-status'},status,rendererStatus),mapPanel,evidencePanel,el('footer',{class:'code-map-footer'},legend));
  const model=createCodeMapModel({request:requestMap,getScope:identity,isCurrent:current,changed:value=>{
    mapData=value;
    if(selected&&!value.nodes.some(n=>n.id===selected))selected=null;
    if(scene){
      if(value.status==='loading'){
        // A pending query does not own the prior result's root. Keep only an
        // accepted projection under this exact still-current authority.
        if(!value.authority||sceneAuthorityKey!==codeMapKey(value.authority)){scene.clear();sceneKey=null;sceneAuthorityKey=null;sceneQuery=null;}
        scene.select(selected);
      }else{
        const visible=codeMapVisible(value,query),key=codeMapKey({authority:value.authority,nodes:visible.nodes.map(n=>n.id),edges:visible.edges.map(e=>e.id)});
        const changed=key!==sceneKey,completed=value.status==='ready',newQuery=completed&&query!==sceneQuery;
        if(changed){sceneKey=key;scene.update(visible);}
        sceneAuthorityKey=completed?codeMapKey(value.authority):null;sceneQuery=completed?query:null;
        scene.select(selected);if(changed||newQuery)revealSelection();
      }
    }
    paint();
  }});
  scene=sceneFactory({mount:viewport,id:'copal-code-map-'+crypto.randomUUID(),onSelect:selection=>{
    if(!current())return;
    if(selection===null){stopRequestedFocus();selected=null;actionSerial++;actionAborter.abort();actionAborter=new AbortController();paintList();paintSelection();}
    else if(selection.authorityNamespace===codeMapKey(mapData.authority))select(selection.id);
  },onChange:value=>{if(current()){
    rendererDiagnostic={status:value.status,backend:value.backend,accelerated:value.accelerated,disposed:value.disposed,drawn:value.drawn?{...value.drawn}:null,density:value.density?{...value.density}:null};
    const renderer=value.status==='layout-error'?'Layout unavailable':value.backend==='webgl2'?'WebGL renderer':value.backend==='canvas2d'?'Software renderer':'Renderer preparing';rendererStatus.textContent=renderer+(value.density?.mode==='overflow'?` · ${value.density.visibleNodes}/${value.density.totalNodes} visible · pan or Explore`:'');
  }}});
  const applyPalette=()=>{if(current()&&typeof getComputedStyle==='function')scene.setPalette?.(codeMapThemePalette(getComputedStyle(element)));};
  const themeObserver=new MutationObserver(applyPalette);
  for(const target of [document.documentElement,document.body])if(target)themeObserver.observe(target,{attributes:true,attributeFilter:['class','style']});
  applyPalette();
  const resize=()=>{if(current()){const r=viewport.getBoundingClientRect();if(r.width>0&&r.height>0){const width=viewport.clientWidth||r.width;scene.resize(width,viewport.clientHeight||r.height,Math.min(2,(window.devicePixelRatio||1)*r.width/width));if(selected)revealSelection();}}};
  const resizeObserver=new ResizeObserver(resize);resizeObserver.observe(viewport);
  const observer=new MutationObserver(()=>{if(!element.isConnected)dispose();});observer.observe(document.body,{childList:true,subtree:true});
  function invalidate(){epoch++;actionSerial++;actionAborter.abort();actionAborter=new AbortController();capabilityAborter.abort();capabilityAborter=new AbortController();selected=null;query=null;sceneKey=null;listPage=0;capability={available:false};capabilityKey=null;capabilityPending=null;model.reset();scene?.clear();legacy?.dispose();legacy=null;evidencePanel.replaceChildren();subscribers.clear();}
  function notifyEvidence(){for(const onStatus of subscribers)onStatus(copy(index));}
  function startIndex(){
    const ticket=++indexSerial,id=workspace;release?.();release=null;loadedKey=null;invalidate();index={lifecycle:id?'starting':'missing'};paint();
    if(!id||!current()||!subscribeIndex)return;
    const owned=subscribeIndex(id,{isCurrent:()=>current()&&ticket===indexSerial&&id===workspace,onStatus:value=>{
      if(!current()||ticket!==indexSerial||id!==workspace)return;
      const before=codeMapIndexKey(index);index=value||{lifecycle:'unavailable'};
      const revoked=['erased','denied','missing'].includes(index.lifecycle),key=codeMapIndexKey(index);
      if(revoked||before!==key){invalidate();loadedKey=null;}
      notifyEvidence();paint();
      if(!revoked&&codeMapMayRead(index)&&loadedKey!==key){
        loadedKey=key;const pending=loadMap('overview'),overviewQuery=query;
        void Promise.resolve(pending).then(completed=>{
          if(completed!==true||!focusPath||!current()||ticket!==indexSerial||id!==workspace||loadedKey!==key||query!==overviewQuery)return;
          return focusRequestedPath({operation:overviewQuery.operation,status:mapData.status,authority:copy(mapData.authority),root:mapData.root,page:copy(mapData.page),evicted:mapData.evicted});
        });
      }
    }});
    if(!current()||ticket!==indexSerial)owned?.();else release=owned;paint();
  }
  function stopRequestedFocus(){focusSerial++;focusPath='';focusMessage='';}
  function selectWorkspace(id,{label:nextLabel=id}={}){if(!current())return;stopRequestedFocus();folderSerial++;workspace=id;label=nextLabel;workspaceName.textContent=label;setWorkspace(id,label);showView('map');startIndex();}
  async function focusRequestedPath(initialOverview=null){
    if(!current()||!focusPath||mapData.status!=='ready')return;
    const key=codeMapKey(mapData.authority);if(key===focusKey)return;focusKey=key;
    const ticket=++focusSerial,owned=()=>current()&&ticket===focusSerial&&codeMapKey(mapData.authority)===key;
    const result=await resolveCodeMapPath(focusPath,{read:(op,root,more)=>loadMap(op,{root,more}),snapshot:()=>model.snapshot(),isCurrent:owned,initialOverview});
    if(!owned())return;
    if(result.state==='found'){select(result.node.id,{requested:true});focusMessage=mapData.authority?.variant==='structure_preview'?'Selected saved source in folder structure; evidence is not published.':'Selected saved/on-disk source.';}
    else focusMessage=['limited','label_limit'].includes(result.state)?'The selected path exceeds the map navigation or label limit. No substitute was selected.':'The exact selected path is not available in this map. No substitute was selected.';
    status.textContent=focusMessage;
  }
  function loadMap(operation,{root=null,more=false}={}){
    if(!current()||!workspace||!codeMapMayRead(index))return false;
    actionSerial++;actionAborter.abort();actionAborter=new AbortController();
    if(!more)query={schema:MAP_SCHEMA,operation,root,expected_authority:operation==='overview'?null:copy(mapData.authority)};
    if(!query)return false;
    return model.run(workspace,query,{more});
  }
  function select(id,{requested=false}={}){
    if(!current())return;const node=mapData.nodes?.find(n=>n.id===id);if(!node)return;
    if(!requested){focusSerial++;focusPath='';focusMessage='';}
    selected=id;actionSerial++;actionAborter.abort();actionAborter=new AbortController();scene.select(id);togglePanel('inspector',true);paintList();paintSelection();
    if(node.kind==='file'&&mapData.authority?.variant==='published')void loadMap('neighbors',{root:{kind:'visual',id}});
  }
  function showView(nextView){
    view=nextView;mapPanel.hidden=view!=='map';evidencePanel.hidden=view!=='evidence';mapButton.setAttribute('aria-pressed',String(view==='map'));evidenceButton.setAttribute('aria-pressed',String(view==='evidence'));
    syncVisibility();
    if(view==='evidence')ensureEvidence();
  }
  function syncVisibility(){if(scene){if(hostVisible&&view==='map'){scene.resume();resize();}else scene.suspend();}}
  function setVisible(value){if(disposed||hostVisible===(value===true))return;hostVisible=value===true;syncVisibility();}
  function ensureEvidence(){
    if(legacy||!mountEvidence)return legacy;
    const panel=el('div');evidencePanel.append(panel);
    legacy=mountEvidence({...options,element:panel,requestMap:null,initialQuery:false,workspaceId:workspace,workspaceLabel:label,
      isCurrent:current,setWorkspace:(id,nextLabel)=>selectWorkspace(id,{label:nextLabel}),
      subscribeIndex:(_id,{onStatus})=>{subscribers.add(onStatus);queueMicrotask(()=>{if(current()&&subscribers.has(onStatus))onStatus(copy(index));});
        const off=()=>subscribers.delete(onStatus);off.refresh=()=>release?.refresh?.();off.readmit=()=>release?.readmit?.();return off;}});
    return legacy;
  }
  async function evidence(operation,extra={}){
    if(!current()||!indexPresentation(index).usable)return false;
    showView('evidence');const active=ensureEvidence();await Promise.resolve();
    return current()&&legacy===active?active?.run(operation,extra):false;
  }
  function togglePanel(which,open){
    const panel=which==='explorer'?explorer:inspector,toggle=which==='explorer'?exploreToggle:inspectToggle;
    const hadFocus=panel.contains(document.activeElement);
    panel.hidden=open===undefined?!panel.hidden:!open;toggle.setAttribute('aria-expanded',String(!panel.hidden));
    if(panel.hidden&&hadFocus)toggle.focus();
    if(!panel.hidden&&(element.clientWidth||element.getBoundingClientRect().width)<760){const other=which==='explorer'?inspector:explorer,otherToggle=which==='explorer'?inspectToggle:exploreToggle;
      const movingFocus=other.contains(document.activeElement);other.hidden=true;otherToggle.setAttribute('aria-expanded','false');
      if(movingFocus)panel.querySelector('.code-map-panel-heading button')?.focus();}
    revealSelection();
  }
  function revealSelection(){
    if(!scene||!selected||view!=='map')return;
    const r=viewport.getBoundingClientRect(),insets={left:12,top:12,right:12,bottom:12};
    if(!r.width||!r.height)return;
    const sx=(viewport.clientWidth||r.width)/r.width,sy=(viewport.clientHeight||r.height)/r.height;
    for(const panel of [explorer,inspector])if(!panel.hidden){
      const p=panel.getBoundingClientRect();
      if(p.width>=r.width*.75&&p.height<r.height*.75)insets.bottom=Math.max(insets.bottom,(r.top+r.height-p.top)*sy+12);
      else if(panel===explorer)insets.left=Math.max(insets.left,(p.left+p.width-r.left)*sx+12);
      else insets.right=Math.max(insets.right,(r.left+r.width-p.left)*sx+12);
    }
    const b=breadcrumbs.getBoundingClientRect();insets.bottom=Math.max(insets.bottom,b.height*sy+18);
    const contextIds=[];
    if(query?.operation==='neighbors'){
      const priority=(mapData.edges||[]).filter(e=>e.relation!=='containment'&&(e.from===selected||e.to===selected))
        .sort((a,b)=>(a.relation!=='import')-(b.relation!=='import')||(a.state!=='exact')-(b.state!=='exact')||b.count-a.count||a.id.localeCompare(b.id));
      for(const e of priority){const id=e.from===selected?e.to:e.from;if(id!==selected&&!contextIds.includes(id)&&mapData.nodes.some(n=>n.id===id&&n.kind==='file'))contextIds.push(id);if(contextIds.length===32)break;}
    }
    scene.revealSelected?.({insets,contextIds});
  }
  function paintList(){
    const focused=list.contains(document.activeElement)?document.activeElement?.dataset.codeMapId:null;
    const rows=codeMapVisible(mapData,query).nodes;listPage=Math.min(listPage,Math.max(0,Math.ceil(rows.length/100)-1));const start=listPage*100;
    list.replaceChildren(...rows.slice(start,start+100).map((n,i)=>button(`${n.label} · ${n.kind}`,()=>select(n.id),{'data-code-map-id':n.id,role:'option','aria-selected':String(n.id===selected),tabindex:i===0?0:-1})));
    if(focused){const items=[...list.querySelectorAll('[data-code-map-id]')],target=items.find(n=>n.dataset.codeMapId===focused);
      if(target){for(const item of items)item.tabIndex=-1;target.tabIndex=0;target.focus();}}
    listLabel.textContent=`${rows.length?start+1:0}–${Math.min(rows.length,start+100)} of ${rows.length} loaded`;
    previous.disabled=listPage===0;next.disabled=start+100>=rows.length;more.disabled=mapData.status!=='ready'||!mapData.page?.next_cursor;
  }
  function paintSelection(){
    const node=mapData.nodes?.find(n=>n.id===selected),selection=mapData.selection;
    inspectorTitle.textContent=node?.label||'Select a folder, file or relationship';
    inspectorSummary.textContent=node?`${node.source_files} source files · ${node.asset_files} assets${mapData.authority?.variant==='structure_preview'?' · structure only; not evidence':''}`:'';
    drill.hidden=!node||node.kind==='file';drill.disabled=!node||mapData.status!=='ready';neighbors.disabled=!node||mapData.status!=='ready';
    const canonical=selection?.visual_id===selected&&mapData.authority?.variant==='published'&&mapData.status==='ready'&&indexPresentation(index).usable;
    inspectEvidence.disabled=!canonical;
    for(const control of evidenceActions)control.disabled=!canonical||!capability.available||!onEvidenceAction;
    const edges=(mapData.edges||[]).filter(e=>e.relation!=='containment'&&(!selected||e.from===selected||e.to===selected));
    edgeList.replaceChildren(el('strong',{text:`Relationships (${edges.length} loaded)`}),...edges.slice(0,50).map(e=>button(`${e.relation} · ${e.state} · ${e.count}`,()=>void model.selectAggregate(e.selection_id))));
    if(edges.length>50)edgeList.append(el('span',{text:`${edges.length-50} more loaded relationships are outside this inspector.`}));
    const aggregate=mapData.aggregate;referenceList.replaceChildren();
    if(aggregate){referenceList.append(el('strong',{text:`Underlying references (${aggregate.references.length}${aggregate.complete?'':', more remain'})`}),
      ...aggregate.references.map(r=>button(`${r.relation} · ${r.state} · inspect occurrence`,()=>evidence('neighbors',{seed_ids:[r.occurrence_id],direction:'both'}))));
      if(aggregate.next_cursor&&!aggregate.clientLimited)referenceList.append(button('More references',()=>void model.selectAggregate(aggregate.selection_id,{cursor:aggregate.next_cursor})));
    }
    const path=[],seen=new Set();let at=mapData.nodes?.find(n=>n.id===mapData.root);
    while(at&&!seen.has(at.id)&&path.length<64){path.unshift(at);seen.add(at.id);at=mapData.nodes.find(n=>n.id===at.parent);}
    breadcrumbs.replaceChildren(...path.map(n=>button(n.label,()=>{stopRequestedFocus();return n.kind==='repository'?loadMap('overview'):loadMap('children',{root:{kind:'visual',id:n.id}});} )));
  }
  function paint(){
    if(!current())return;status.textContent=[codeMapStatus(index,mapData),focusMessage,options.savedSourceNotice?'Graph represents saved/on-disk source; unsaved Editor changes were not saved or indexed.':''].filter(Boolean).join(' ');status.dataset.recovering=String(index.recovering===true);
    const usable=indexPresentation(index).usable;search.disabled=searchButton.disabled=evidenceButton.disabled=!usable;
    overview.disabled=!codeMapMayRead(index)||mapData.status==='loading';mapReload.disabled=!codeMapMayRead(index)||mapData.status==='loading';
    recheck.disabled=!release;retry.hidden=!['missing','failed','cancelled','unavailable'].includes(index.lifecycle)&&mapData.error?.code!=='reindex_required';retry.disabled=!workspace;readmit.hidden=index.lifecycle!=='erased';readmit.disabled=!release;detach.disabled=!release;
    empty.hidden=(mapData.nodes?.length||0)>0;empty.querySelector('span').textContent=status.textContent;
    const diagnosticText=JSON.stringify({index:codeMapIndexDiagnostics(index),map:{authority:mapData.authority,coverage:mapData.coverage,page:mapData.page,error:mapData.error,evicted:mapData.evicted,retainedBytes:mapData.retainedBytes},renderer:rendererDiagnostic},null,2);
    if(diagnostics.textContent!==diagnosticText)diagnostics.textContent=diagnosticText;
    paintList();paintSelection();
    if(usable&&mapData.authority?.variant==='published')void loadCapabilities();
  }
  async function loadCapabilities(){
    const key=codeMapKey(mapData.authority);if(capabilityKey===key||capabilityPending===key)return;capabilityPending=key;
    const id=workspace,ticket=epoch;
    try{const value=await readResourceCapabilities?.(id,capabilityAborter.signal);if(current()&&id===workspace&&ticket===epoch&&key===codeMapKey(mapData.authority)){capability=value?.available===true?value:{available:false};capabilityKey=key;}}
    catch{if(current()&&ticket===epoch)capability={available:false};}
    finally{if(capabilityPending===key)capabilityPending=null;if(current()&&ticket===epoch)paintSelection();}
  }
  async function act(kind){
    const selection=mapData.selection;if(!current()||mapData.status!=='ready'||!selection||selection.visual_id!==selected||!capability.available||!indexPresentation(index).usable)return;
    const receipt=copy(selection.receipt),id=selection.resource_id,workspaceId=workspace,authority=codeMapKey(mapData.authority),ticket=++actionSerial;
    const valid=()=>current()&&ticket===actionSerial&&workspace===workspaceId&&mapData.selection?.resource_id===id&&mapData.selection?.visual_id===selected&&codeMapKey(mapData.authority)===authority&&indexPresentation(index).usable;
    for(const b of evidenceActions)b.disabled=true;
    try{const result=await onEvidenceAction?.(kind,{node:{id,kind:'file'},receipt,workspaceId,isCurrent:valid,signal:actionAborter.signal});if(valid())status.textContent=result?.message||'Source action completed.';}
    catch{if(valid())status.textContent='Current source action unavailable.';}
    finally{if(valid())paintSelection();}
  }
  listen(search,'keydown',event=>{if(event.key==='Enter'){
    event.preventDefault();details.open=false;details.querySelector('summary').focus();
    void evidence('search',{query:search.value||undefined});
  }});
  listen(details,'click',event=>{if(event.target?.tagName==='BUTTON'){
    const restore=details.contains(document.activeElement);details.open=false;if(restore)details.querySelector('summary').focus();
  }});
  listen(list,'keydown',event=>{
    const items=[...list.querySelectorAll('[data-code-map-id]')],at=items.indexOf(document.activeElement),last=items.length-1;
    const next=({ArrowDown:Math.min(last,at+1),ArrowUp:Math.max(0,at-1),Home:0,End:last})[event.key];
    if(next===undefined||!items[next])return;event.preventDefault();for(const item of items)item.tabIndex=-1;items[next].tabIndex=0;items[next].focus();
  });
  listen(element,'keydown',event=>{if(event.key==='Escape'&&(details.open||!explorer.hidden||!inspector.hidden)){
    event.stopPropagation();if(details.open&&details.contains(document.activeElement))details.querySelector('summary').focus();
    details.open=false;togglePanel('explorer',false);togglePanel('inspector',false);}});
  listen(preference,'change',async()=>{const ticket=++preferenceSerial,value=preference.checked;preference.disabled=true;
    try{const saved=await writePreference?.(value);if(current()&&ticket===preferenceSerial){preference.checked=saved===true;preferenceText.textContent=saved===true?'Automatic context enabled.':'Automatic context is off.';}}
    catch{if(current()&&ticket===preferenceSerial){preference.checked=false;preferenceText.textContent='Setting unavailable; automatic context is off.';}}
    finally{if(current()&&ticket===preferenceSerial)preference.disabled=!writePreference;}});
  const prefTicket=++preferenceSerial;Promise.resolve().then(()=>readPreference?.()).then(value=>{if(current()&&prefTicket===preferenceSerial){preference.checked=value===true;preference.disabled=!writePreference;preferenceText.textContent=value===true?'Automatic context enabled.':'Automatic context is off.';}},()=>{});
  resize();paint();if(workspace)startIndex();
  function dispose(){if(disposed)return;disposed=true;indexSerial++;folderSerial++;actionSerial++;preferenceSerial++;release?.();release=null;actionAborter.abort();capabilityAborter.abort();aborter.abort();model.dispose();legacy?.dispose();scene.dispose();resizeObserver.disconnect();observer.disconnect();themeObserver.disconnect();subscribers.clear();}
  return {run:(operation,extra={})=>operation==='overview'?loadMap('overview'):evidence(operation,extra),selectWorkspace,isCurrent:current,isMounted:()=>!disposed&&element.isConnected,
    snapshot:()=>view==='evidence'&&legacy?legacy.snapshot():model.snapshot(),camera:()=>scene.snapshot().camera,presentationStats:()=>scene.snapshot(),setVisible,dispose};
}
