// Compact app-only geometry with the existing evidence workbench on demand.
// One index subscriber owns admission/observation; visual reads never index.
import { resolveGraphSettings, normalizeGraphSettings, mountGraphSettings } from './graphSettings.js';
import { MAP_RELATIONS, MAP_SCHEMA, codeMapKey, createCodeMapModel } from './codeMapModel.js';
import { createCodeMapScene } from './codeMapScene.js';
import { indexPresentation, indexUpdateLabel, indexPhaseLabel } from './codeIndexObserver.js';
import { resolveCodeMapPath } from './codeMapPath.js';
import { filesFacadeClient } from '../filesFacadeClient.js';

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
  return codeMapKey(Object.fromEntries(['registration_id','admission_epoch','policy_generation','head_generation','config_fingerprint'].map(k=>[k,index[k]??null])));
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
  out.progress={};for(const key of ['completed_files','total_files','heartbeat_at','phase','phase_completed','phase_total','phase_unit','phase_observed_at']){const value=scalar(index.progress?.[key]);if(value!==undefined)out.progress[key]=value;}
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
function descendantOf(id,ancestor,byId){
  const seen=new Set();let parent=byId.get(id)?.parent;
  while(parent&&!seen.has(parent)&&seen.size<64){if(parent===ancestor)return true;seen.add(parent);parent=byId.get(parent)?.parent;}
  return false;
}
// Visibility belongs to explicit expansion, never the latest query's root.
export function codeMapVisible(data={},exploration={}) {
  const nodes=data.nodes||[],edges=data.edges||[],byId=new Map(nodes.map(n=>[n.id,n]));
  const root=exploration.anchor||nodes.find(n=>n.kind==='repository')?.id||data.root||null;
  const expanded=exploration.expanded||new Set(root?[root]:[]),contexts=exploration.contexts||new Set();
  const hidden=exploration.hiddenChildren||new Set();
  const relations=exploration.relations||new Set(MAP_RELATIONS),ids=new Set(root?[root]:[]);
  let changed=true;for(let depth=0;changed&&depth<64;depth++){changed=false;for(const n of nodes)if(ids.has(n.parent)&&expanded.has(n.parent)&&!ids.has(n.id)){ids.add(n.id);changed=true;}}
  for(const edge of edges)if(edge.relation!=='containment'&&relations.has(edge.relation)&&(contexts.has(edge.from)||contexts.has(edge.to))){ids.add(edge.from);ids.add(edge.to);}
  for(const id of [...ids]){let parent=byId.get(id)?.parent;const seen=new Set();while(parent&&byId.has(parent)&&!seen.has(parent)&&seen.size<64){seen.add(parent);ids.add(parent);parent=byId.get(parent).parent;}}
  // A hidden subtree wins over outside relationship contexts and late cache
  // merges. With a mask active, incomplete/cyclic ancestry cannot prove a
  // context endpoint lies outside it; keep only known paths to the real root.
  if(hidden.size)for(const id of [...ids]){
    let at=id;const seen=new Set();let allowed=false;
    for(let depth=0;depth<=64;depth++){
      if(at===root){allowed=true;break;}
      if(!byId.has(at)||seen.has(at))break;seen.add(at);
      const parent=byId.get(at).parent;if(parent===null||hidden.has(parent))break;at=parent;
    }
    if(!allowed)ids.delete(id);
  }
  return {...data,root,nodes:nodes.filter(n=>ids.has(n.id)),edges:edges.filter(e=>relations.has(e.relation)&&ids.has(e.from)&&ids.has(e.to))};
}
export function codeMapStatus(index={},map={}) {
  const lifecycle=index.lifecycle||'missing',presentation=indexPresentation(index),pending=presentation.pending;
  const codes={denied:'Access denied. Choose an authorized folder.',erased:'This index was erased. Readmit it explicitly to start again.',stale_cursor:'The code index changed. Reload the map.',reindex_required:'This older index needs an explicit rebuild.',budget_exceeded:'This map exceeds the current reading budget. Choose a smaller code folder.',busy:'The index is busy. Retry the map shortly.',cancelled:'Map loading was cancelled.'};
  if(['denied','erased'].includes(lifecycle))return codes[lifecycle];
  if(map.status==='error'){
    const message=codes[map.error?.code]||'Code map unavailable. Retry or inspect details.';
    return pending?[presentation.text,map.error?.code==='reindex_required'?'Previous map unavailable; waiting for this index.':message].join(' · '):message;
  }
  if(lifecycle==='unavailable')return index.recovering?'Index connection interrupted; rechecking status.':'Index connection unavailable. Recheck status.';
  if(map.authority?.variant==='structure_preview'){
    const p=index.progress||{};
    const lifecycleText=lifecycle==='failed'?'Indexing failed':lifecycle==='cancelled'?'Indexing cancelled':pending?'Indexing':'No compatible published dependency head';
    return ['Folder preview · dependencies not yet published',lifecycleText,indexPhaseLabel(p),indexUpdateLabel(p.heartbeat_at),
      index.freshness?`Freshness: ${index.freshness}`:'',index.error?.message?String(index.error.message):'',map.coverage?.inventory_complete?'':'Partial inventory'].filter(Boolean).join(' · ');
  }
  if(map.status==='ready') {
    if(!map.nodes?.length)return 'No visible code or folders in this scope.';
    if(pending)return ['Previous published index',indexPhaseLabel(index.progress),indexUpdateLabel(index.progress?.heartbeat_at)].filter(Boolean).join(' · ');
    if(lifecycle==='cancelled')return 'Previous index · verification cancelled. Retry indexing to continue.';
    if(lifecycle==='failed')return 'Previous index · rebuild failed. Source actions revalidate current files.';
    return map.coverage?.dependencies==='partial'?'Code map · partial dependency coverage':'Code map ready';
  }
  if(pending)return [indexPhaseLabel(index.progress)||'Indexing code','Loading folder structure',indexUpdateLabel(index.progress?.heartbeat_at)].filter(Boolean).join(' · ');
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
  let index={lifecycle:'missing'},release=null,loadedKey=null,mapData={status:'idle',authority:null,nodes:[],edges:[]},selected=null,query=null,view='map',listPage=0,hostVisible=true;
  let sceneByteNotice='',legacy=null,scene=null,sceneKey=null,sceneAuthorityKey=null,sceneQuery=null,rendererDiagnostic=null,capability={available:false},capabilityKey=null,capabilityPending=null;
  let actionAborter=new AbortController(),capabilityAborter=new AbortController();
  let anchor=null,connectionDirection='both',presentation='map',expansionSerial=0,readQueue=Promise.resolve(),layoutPaused=false,refreshSerial=0,refreshing=false,mapFailures=0,nextMapAttempt=0,attemptKey=null,staging=null,suppressModel=false,expansionBusy=false,expansionNotice='';
  let retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};
  const expanded=new Set(),contexts=new Set(),hiddenChildren=new Set(),relations=new Set(MAP_RELATIONS);
  let settingsEnvelope=normalizeGraphSettings(options.readGraphSettings?.()),settings=resolveGraphSettings(settingsEnvelope,presentation),settingsDispose=null,desiredSelected=null;
  const expandable=n=>Boolean(n&&(mapData.authority?.variant==='published'||!['file','symbol'].includes(n.kind)));
  let focusPath=options.focusPath||'',focusKey=null,focusSerial=0,focusMessage='';
  const subscribers=new Set(),aborter=new AbortController(),signal=aborter.signal;
  const current=()=>!disposed&&isCurrent()&&element.isConnected;
  const listen=(node,name,fn,opts={})=>node.addEventListener(name,fn,{...opts,signal});
  const button=(text,fn,attrs={})=>{const b=el('button',{type:'button',class:'copal-btn',text,...attrs});listen(b,'click',fn);return b;};
  const identity=()=>({workspace,epoch});
  element.classList.add('code-map-workbench');element.dataset.codeGraph='true';
  const workspaceName=el('strong',{'data-code-workspace':true,text:label||workspace||'No code folder selected'});
  const presentationSelect=el('select',{'aria-label':'Code Graph presentation'},...['map','web','gravity'].map(value=>el('option',{value,text:value[0].toUpperCase()+value.slice(1)})));
  listen(presentationSelect,'change',()=>{presentation=presentationSelect.value;settingsEnvelope.presentation=presentation;options.writeGraphSettings?.(settingsEnvelope);showView('map');applySettings();scene?.setPresentation(presentation);});
  const folder=button('Choose code folder',async()=>{
    const ticket=++folderSerial;
    try{const binding=await chooseWorkspace?.();if(current()&&ticket===folderSerial&&binding?.id)selectWorkspace(binding.id,{label:binding.name||binding.path||binding.id});}
    catch{if(current()&&ticket===folderSerial)status.textContent='Code folder unavailable. Choose an authorized folder.';}
  });
  const search=el('input',{type:'search',maxlength:1024,'aria-label':'Search scoped code',placeholder:'Search code…'});
  const searchButton=button('Search',()=>evidence('search',{query:search.value||undefined}));
  const overview=button('Overview',()=>{stopRequestedFocus();showView('map');return initialExpansion();},{'data-code-operation':'overview'});
  const exploreToggle=button('Explore',()=>togglePanel('explorer'),{'aria-expanded':'false','aria-controls':'code-map-explorer-'+crypto.randomUUID()});
  const inspectToggle=button('Inspector',()=>togglePanel('inspector'),{'aria-expanded':'false','aria-controls':'code-map-inspector-'+crypto.randomUUID()});
  const mapButton=button('Map',()=>showView('map'),{'aria-pressed':'true'});
  const evidenceButton=button('Search & evidence',()=>{showView('evidence');if(indexPresentation(index).usable&&!legacy?.snapshot().nodes.length)evidence('catalog');},{'aria-pressed':'false'});
  const status=el('span',{'data-code-index-status':true,role:'status','aria-live':'polite'});
  const rendererStatus=el('span',{'data-code-renderer':true,text:'Preparing renderer'});
  const recheck=button('Refresh now',()=>index.head_generation||index.previous_head_generation?release?.verify?.():release?.refresh?.());
  const retry=button('Retry indexing',()=>startIndex({preserve:Boolean(mapData.authority)}));
  const readmit=button('Readmit erased index',()=>{if(index.lifecycle==='erased')void release?.readmit?.();});
  const detach=button('Stop observing',()=>{indexSerial++;release?.();release=null;invalidate();index={lifecycle:'unavailable'};notifyEvidence();paint();});
  const preference=el('input',{type:'checkbox','aria-label':'Automatic code context',disabled:true});
  const preferenceText=el('span',{text:'Automatic context is off until verified.'});
  const diagnostics=el('pre',{'data-code-diagnostics':true});
  const settingsPanel=el('section',{class:'code-map-diagnostics',hidden:true,'aria-label':'Graph settings'});
  const diagnosticPanel=el('section',{class:'code-map-diagnostics',hidden:true,'aria-label':'Code Graph coverage and diagnostics'},diagnostics);
  const header=el('header',{class:'code-map-header'},el('span',{class:'code-map-alpha',text:'Code Graph · Alpha'}),workspaceName);
  const toolbar=el('nav',{class:'code-map-toolbar code-map-primary-tools','aria-label':'Code map presentation'},el('label',{},'Presentation ',presentationSelect));
  const viewport=el('div',{class:'code-map-viewport','data-code-viewport':true});
  const empty=el('div',{class:'code-map-empty','data-code-empty':true});
  const mapReload=button('Reload map',()=>{stopRequestedFocus();return retryMapRead();});
  empty.append(el('span',{text:'Choose a code folder to see its structure.'}),mapReload);
  const list=el('div',{class:'code-map-list',role:'tree','aria-label':'Code map resources'});
  const listLabel=el('span',{'data-code-map-list-count':true});
  const previous=button('Previous',()=>{listPage=Math.max(0,listPage-1);paintList();});
  const next=button('Next',()=>{listPage++;paintList();});
  const more=button('Load more',()=>loadBranchMore());
  const explorer=el('aside',{id:exploreToggle.getAttribute('aria-controls'),class:'code-map-explorer','aria-label':'Code map explorer',hidden:true},
    el('div',{class:'code-map-panel-heading'},el('strong',{text:'Folders & files'}),button('Close explorer',()=>togglePanel('explorer',false))),
    list,listLabel,el('div',{class:'code-map-toolbar'},previous,next),more);
  const inspectorTitle=el('strong',{text:'Select a folder, file or relationship'}),inspectorSummary=el('p');
  const drill=button('Show children',()=>selected&&toggleBranchChildren(selected));
  const neighbors=button('Connections',()=>selected&&connect(selected));
  const inspectEvidence=button('Inspect evidence',()=>{const binding=model.bindingFor(selected);if(binding)return evidence('outline',{seed_ids:[binding.resource_id]});});
  const evidenceActions=['open','add','ask'].map((kind,n)=>button(['Open source','Add to chat','Ask about this'][n],()=>act(kind)));
  const edgeList=el('div',{class:'code-map-relationships'}),referenceList=el('div',{class:'code-map-references'});
  const inspector=el('aside',{id:inspectToggle.getAttribute('aria-controls'),class:'code-map-inspector','aria-label':'Selected code map item',hidden:true},
    el('div',{class:'code-map-panel-heading'},inspectorTitle,button('Close inspector',()=>togglePanel('inspector',false))),inspectorSummary,
    el('div',{class:'code-map-toolbar'},drill,neighbors,inspectEvidence,...evidenceActions),edgeList,referenceList);
  const mapPanel=el('section',{class:'code-map-panel','aria-label':'Code map'},viewport,empty,explorer,inspector);
  const breadcrumbs=el('nav',{class:'code-map-breadcrumbs','aria-label':'Code folder path'});mapPanel.append(breadcrumbs);
  const evidencePanel=el('section',{class:'code-map-evidence','aria-label':'Code search and detailed evidence',hidden:true});
  const legend=el('span',{text:'Files and symbols · typed static relationships, not execution'});
  element.append(header,toolbar,el('div',{class:'code-map-status'},status,rendererStatus),mapPanel,evidencePanel,diagnosticPanel,settingsPanel,el('footer',{class:'code-map-footer'},legend));
  const modelOptions={request:requestMap,getScope:identity,isCurrent:current,getPinned:()=>[anchor,selected].filter(Boolean)};
  let model=createCodeMapModel({request:requestMap,getScope:identity,isCurrent:current,getPinned:()=>[anchor,selected].filter(Boolean),changed:value=>{
    if(suppressModel)return;
    if(mapData.authority&&value.authority&&codeMapKey(mapData.authority)!==codeMapKey(value.authority)){desiredSelected=selected;contexts.clear();selected=null;}
    if(!value.authority){anchor=null;expanded.clear();contexts.clear();hiddenChildren.clear();}
    mapData=value;rebindRetainedIntent();
    if(value.status==='ready'){const retained=new Set(value.nodes.map(n=>n.id));/* Expansion intent is not a proof of retained adjacency. */for(const id of contexts)if(!retained.has(id))contexts.delete(id);/* Hidden intent survives partial successor pages; it is bounded by retained-cache capacity. */}
    if(value.status==='ready'&&!anchor){anchor=value.nodes.find(n=>n.kind==='repository')?.id||value.root; if(anchor)expanded.add(anchor);}
    if(desiredSelected&&value.nodes.some(n=>n.id===desiredSelected)){selected=desiredSelected;desiredSelected=null;}
    if(selected&&!value.nodes.some(n=>n.id===selected))selected=null;
    updateScene();
    paint();
  }});
  scene=sceneFactory({settings,mount:viewport,id:'copal-code-map-'+crypto.randomUUID(),onSelect:selection=>{
    if(!current())return;
    if(selection===null){stopRequestedFocus();retainedIntent.selection=null;selected=null;actionSerial++;actionAborter.abort();actionAborter=new AbortController();paintList();paintSelection();}
    else if(selection.authorityNamespace===codeMapKey(mapData.authority))select(selection.id);
  },onChange:value=>{if(current()){
    rendererDiagnostic={status:value.status,backend:value.backend,accelerated:value.accelerated,disposed:value.disposed,drawn:value.drawn?{...value.drawn}:null,density:value.density?{...value.density}:null,renderPolicy:value.renderPolicy?{...value.renderPolicy}:null};
    const renderer=value.status==='layout-error'?'Layout unavailable':value.backend==='webgl2'?'WebGL renderer':value.backend==='canvas2d'?'Software renderer':'Renderer preparing';rendererStatus.textContent=renderer+(value.density?.mode==='overflow'?` · ${value.density.visibleNodes}/${value.density.totalNodes} visible · pan or Explore`:'');
  }}});
  const applyPalette=()=>{if(current()&&typeof getComputedStyle==='function')scene.setPalette?.(codeMapThemePalette(getComputedStyle(element)));};
  const themeObserver=new MutationObserver(applyPalette);
  for(const target of [document.documentElement,document.body])if(target)themeObserver.observe(target,{attributes:true,attributeFilter:['class','style']});
  applyPalette();
  const resize=()=>{if(current()){const r=viewport.getBoundingClientRect();if(r.width>0&&r.height>0){const width=viewport.clientWidth||r.width;scene.resize(width,viewport.clientHeight||r.height,Math.min(settings.dpr,(window.devicePixelRatio||1)*r.width/width));if(selected)revealSelection();}}};
  const resizeObserver=new ResizeObserver(resize);resizeObserver.observe(viewport);
  const observer=new MutationObserver(()=>{if(!element.isConnected)dispose();});observer.observe(document.body,{childList:true,subtree:true});
  function invalidate(){refreshSerial++;staging?.dispose();staging=null;refreshing=false;epoch++;sceneAuthorityKey=null;desiredSelected=null;retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};settingsPanel.hidden=true;settingsDispose?.();settingsDispose=null;expansionSerial++;anchor=null;expanded.clear();contexts.clear();hiddenChildren.clear();expansionBusy=false;expansionNotice='';actionSerial++;actionAborter.abort();actionAborter=new AbortController();capabilityAborter.abort();capabilityAborter=new AbortController();selected=null;query=null;sceneKey=null;listPage=0;capability={available:false};capabilityKey=null;capabilityPending=null;model.reset();scene?.clear();legacy?.dispose();legacy=null;evidencePanel.replaceChildren();subscribers.clear();}
  function notifyEvidence(){for(const onStatus of subscribers)onStatus(copy(index));}
  function readLimits(){return {nodes:settings.pageNodes,edges:settings.pageEdges,groups:settings.pageGroups,bytes:settings.pageKiB*1024,work:settings.queryWork};}
  function configureObservation(){release?.configure?.({automatic:settings.autoRefresh,verifySeconds:settings.verifySeconds,cooldownSeconds:settings.cooldownSeconds,debounceMs:settings.debounceMs});release?.setVisible?.(hostVisible&&!document.hidden);}
  function applySettings(){
    const previous=settings;settings=resolveGraphSettings(settingsEnvelope,presentation);
    const relationKeys={containment:'showContainment',import:'showImport',call:'showCall',value_reference:'showValueReference',type_reference:'showTypeReference',inherits:'showInherits',implements:'showImplements',render:'showRender',route:'showRoute',overrides:'showOverrides'};
    relations.clear();for(const [relation,key]of Object.entries(relationKeys))if(settings[key])relations.add(relation);
    if(model&&(!previous||['retainedNodes','retainedEdges','cacheMiB','pageNodes','pageEdges','pageGroups','pageKiB','queryWork'].some(k=>settings[k]!==previous[k])))model.configure({maxNodes:settings.retainedNodes,maxEdges:settings.retainedEdges,maxBytes:settings.cacheMiB*1048576});
    if(previous&&Object.keys(settings).some(k=>(k.startsWith('evidence')||k.startsWith('breadcrumb')||k.startsWith('search')||k==='queryWork')&&settings[k]!==previous[k]))legacy?.configureSettings?.();
    connectionDirection=settings.direction;configureObservation();scene?.setSettings?.(settings);updateScene();paint();
  }
  function showSettings(){
    settingsPanel.hidden=false;settingsDispose?.();const scope=identity();
    settingsDispose=mountGraphSettings(settingsPanel,{mode:presentation,read:()=>settingsEnvelope,write:value=>{if(current()&&codeMapKey(scope)===codeMapKey(identity())){settingsEnvelope=value;options.writeGraphSettings?.(value);}},isCurrent:()=>current()&&codeMapKey(scope)===codeMapKey(identity()),onChange:applySettings});
    settingsPanel.prepend(button('Close settings',()=>{settingsPanel.hidden=true;}));
  }
  async function focusResource(resource){
    if(!current()||mapData.authority?.variant!=='published'||!resource?.id)return false;
    showView('map');const receipt=resource.receipt||mapData.authority.receipt;
    if(codeMapKey(receipt)!==codeMapKey(mapData.authority.receipt))return false;
    const ticket=++expansionSerial;
    const ok=await loadMap('neighbors',{root:{kind:'resource',resource_id:resource.id,receipt},expansionTicket:ticket});
    if(ok&&ticket===expansionSerial){const binding=mapData.selection;if(binding?.resource_id===resource.id){contexts.add(binding.visual_id);select(binding.visual_id,{requested:true});return true;}}
    return false;
  }
  // Existing successful-save/root-watch hints only. No new watch subscription.
  let sourceValidationRunning=false,pendingSourceHint=null;
  async function validateSourceHints(){
    if(sourceValidationRunning)return;sourceValidationRunning=true;
    try{while(pendingSourceHint&&current()){
      const hint=pendingSourceHint;pendingSourceHint=null;
      const owned=()=>current()&&release===hint.release&&codeMapKey(identity())===hint.scope
        &&index.workspace_revision===hint.revision&&index.policy_generation===hint.policy;
      if(!owned())continue;
      try{
        const target=await filesFacadeClient.codeGraphTarget(hint.ref,{workspaceId:hint.workspace,expectedWorkspaceRevision:hint.revision,signal});
        if(!owned()||target.generation!==hint.policy||(hint.kind==='watch'?target.open_relative!=='':!target.open_relative))continue;
        hint.release.sourceChanged?.(hint.ticket);
      }catch{/* Unavailable, stale or out-of-scope hints never grant a retry. */}
    }}finally{sourceValidationRunning=false;}
  }
  function sourceHint(ref,kind){
    release?.dirty?.();
    const ticket=release?.captureSourceChange?.();
    if(!ticket||typeof ref!=='string'||ref.length<8||ref.length>16384
      ||!Number.isSafeInteger(index.workspace_revision)||!Number.isSafeInteger(index.policy_generation))return;
    // One active validation and one latest pending hint bound event storms.
    pendingSourceHint={ref,kind,ticket,release,workspace,scope:codeMapKey(identity()),revision:index.workspace_revision,policy:index.policy_generation};
    void validateSourceHints();
  }
  listen(document,'visibilitychange',()=>{release?.setVisible?.(hostVisible&&!document.hidden);if(!document.hidden&&hostVisible)release?.dirty?.();});
  listen(window,'focus',()=>{if(hostVisible&&!document.hidden)release?.dirty?.();});
  listen(window,'openclank-files-watch-dirty',event=>{if(event.detail?.provider==='host'&&workspace)sourceHint(event.detail.resourceRef,'watch');});
  listen(window,'online',()=>{if(hostVisible)release?.dirty?.();});
  // Mutation hints never grant authority; ensure verifies bytes in the existing owner.
  listen(window,'openclank-files-resource-mutated',event=>{const resource=event.detail?.resource;if(!resource||(resource.provider||resource.key?.provider)!=='host'||!workspace)return;release?.dirty?.();
    if(event.detail?.action==='save')sourceHint(resource.ref||resource.locator?.opaqueRef||event.detail.resourceRef,'save');
    // Only a typed, successful resource identity may map to visible file nodes.
    const path=resource.path?.relative||resource.relative_path||resource.rel_path;
    if(typeof path==='string'&&resource.workspace_id===workspace){const normalized=path.replaceAll('\\','/');const byId=new Map(mapData.nodes.map(n=>[n.id,n]));const matches=mapData.nodes.filter(n=>n.kind==='file'&&(()=>{const labels=[n.label];let p=byId.get(n.parent),seen=new Set();while(p&&p.kind!=='repository'&&!seen.has(p.id)){seen.add(p.id);labels.unshift(p.label);p=byId.get(p.parent);}return labels.join('/')===normalized;})());scene?.activity?.(matches.map(n=>n.id));}
  });
  function startIndex({preserve=false}={}){
    const ticket=++indexSerial,id=workspace;release?.();release=null;loadedKey=null;if(!preserve)invalidate();index={lifecycle:id?'starting':'missing'};paint();
    if(!id||!current()||!subscribeIndex)return;
    const owned=subscribeIndex(id,{automatic:settings.autoRefresh,isCurrent:()=>current()&&ticket===indexSerial&&id===workspace,onStatus:value=>{
      if(!current()||ticket!==indexSerial||id!==workspace)return;
      const before=codeMapIndexKey(index);index=value||{lifecycle:'unavailable'};
      const revoked=['erased','denied','missing'].includes(index.lifecycle),key=codeMapIndexKey(index);
      if(revoked){invalidate();loadedKey=null;}else if(before!==key){loadedKey=null;actionSerial++;actionAborter.abort();actionAborter=new AbortController();}
      notifyEvidence();paint();
      if(attemptKey!==key){attemptKey=key;mapFailures=0;nextMapAttempt=0;}
      if(!revoked&&codeMapMayRead(index)&&loadedKey!==key&&mapFailures<3&&Date.now()>=nextMapAttempt){
        loadedKey=key;const pending=mapData.authority?refreshPublishedGraph(key):initialExpansion();
        void Promise.resolve(pending).then(completed=>{
          if(completed!==true){if(current()&&ticket===indexSerial&&loadedKey===key){loadedKey=null;mapFailures++;nextMapAttempt=Date.now()+Math.min(30000,1000*2**mapFailures);}return;}
          if(!focusPath||!current()||ticket!==indexSerial||id!==workspace||loadedKey!==key)return;
          return focusRequestedPath();
        });
      }
    }});
    if(!current()||ticket!==indexSerial)owned?.();else release=owned;configureObservation();paint();
  }
  function nodeIntent(id,data=mapData){
    const byId=new Map(data.nodes.map(n=>[n.id,n])),node=byId.get(id);if(!node)return null;
    const path=[],seen=new Set();let n=node;
    while(n&&n.kind!=='repository'&&!seen.has(n.id)&&seen.size<64){seen.add(n.id);if(n.kind!=='symbol'){if(new TextEncoder().encode(n.label).length>=157)return null;path.unshift(n.label);}n=byId.get(n.parent);}
    if(!n||n.kind!=='repository')return null;
    return {kind:node.kind,path,resource:node.resource_id||null};
  }
  const intentKey=value=>value?codeMapKey(value.kind==='symbol'?{kind:value.kind,resource:value.resource}:{kind:value.kind,path:value.path}):'';
  function rebindRetainedIntent(){
    const lookup=new Map(mapData.nodes.map(n=>[intentKey(nodeIntent(n.id)),n]));
    for(const [targets,set] of [[retainedIntent.expanded,expanded],[retainedIntent.hidden,hiddenChildren],[retainedIntent.contexts,contexts]])for(const target of targets){const n=lookup.get(intentKey(target));if(n)set.add(n.id);}
    if(!selected&&retainedIntent.selection){const n=lookup.get(intentKey(retainedIntent.selection));if(n)selected=n.id;}
  }
  async function refreshPublishedGraph(key){
    const ticket=++refreshSerial,scope=identity(),intentRevision=expansionSerial,owned=()=>current()&&intentRevision===expansionSerial&&ticket===refreshSerial&&codeMapKey(scope)===codeMapKey(identity())&&loadedKey===key;
    const old=model,intent={expanded:[...retainedIntent.expanded,...[...expanded].map(id=>nodeIntent(id)).filter(Boolean)],hidden:[...retainedIntent.hidden,...[...hiddenChildren].map(id=>nodeIntent(id)).filter(Boolean)],contexts:[...retainedIntent.contexts,...[...contexts].map(id=>nodeIntent(id)).filter(Boolean)],selection:nodeIntent(selected)||retainedIntent.selection};
    if([...hiddenChildren].some(id=>!nodeIntent(id))){expansionNotice='Cannot safely restore a hidden branch with an incomplete or truncated path. Reset Overview before refreshing.';paint();return false;}
    for(const key of ['expanded','hidden','contexts'])intent[key]=[...new Map(intent[key].filter(Boolean).map(n=>[intentKey(n),n])).values()];
    if(intent.expanded.length+intent.hidden.length+intent.contexts.length>4000||new TextEncoder().encode(JSON.stringify(intent)).length>524288){expansionNotice='Previous branch intent exceeds the refresh budget. Reset Overview or reduce expanded branches before refreshing.';paint();return false;}
    let data={nodes:[],edges:[],authority:null},reads=0;refreshing=true;paint();staging?.dispose();
    const staged=createCodeMapModel({...modelOptions,isCurrent:()=>model===staged?current()&&codeMapKey(scope)===codeMapKey(identity()):owned(),getPinned:()=>model===staged?[anchor,selected].filter(Boolean):[],maxNodes:settings.retainedNodes,maxEdges:settings.retainedEdges,maxBytes:settings.cacheMiB*1048576,changed:value=>{data=value;if(model===staged&&!suppressModel){mapData=value;rebindRetainedIntent();if(selected&&!value.nodes.some(n=>n.id===selected))selected=null;updateScene();paint();}}});staging=staged;
    const read=async(operation,root=null,more=false)=>{if(!owned()||reads>=settings.autoReads)return false;reads++;const ok=await staged.run(workspace,{schema:MAP_SCHEMA,operation,root,expected_authority:operation==='overview'?null:data.authority,limits:readLimits()},{more,missingIsAbsent:root?.kind==='resource'});if(['denied','erased'].includes(data.error?.code)){const code=data.error.code;invalidate();index={lifecycle:code};paint();}return ok;};
    const locate=async target=>{
      if(!target)return null;if(target.resource){let n=data.nodes.find(n=>n.resource_id===target.resource);if(n)return n;if(target.kind==='symbol'&&data.authority?.variant==='published'&&reads<settings.autoReads){await read('neighbors',{kind:'resource',resource_id:target.resource,receipt:data.authority.receipt});n=data.nodes.find(n=>n.resource_id===target.resource);return n||null;}}
      let at=data.nodes.find(n=>n.kind==='repository');if(!at)return null;
      for(const part of target.path){
        let matches=data.nodes.filter(n=>n.parent===at.id&&n.label===part&&n.kind!=='symbol');
        const request=()=>({schema:MAP_SCHEMA,operation:at.kind==='repository'?'overview':'children',root:at.kind==='repository'?null:{kind:'visual',id:at.id},expected_authority:data.authority,limits:readLimits()});
        while(matches.length===0&&reads<settings.autoReads){const q=request(),page=staged.pageFor(q);if(page?.complete)break;if(!await read(q.operation,q.root,Boolean(page?.next_cursor)))break;matches=data.nodes.filter(n=>n.parent===at.id&&n.label===part&&n.kind!=='symbol');}
        if(matches.length!==1)return null;at=matches[0];
      }
      if(target.kind==='symbol'){
        // Declaration identity is not repaired by a convenient label match.
        if(!data.nodes.some(n=>n.resource_id===target.resource)&&reads<settings.autoReads)await read('children',{kind:'visual',id:at.id});
        return data.nodes.find(n=>n.resource_id===target.resource)||null;
      }
      return at.kind===target.kind?at:null;
    };
    try{
      if(!await read('overview')||!owned())return false;
      const reboundExpanded=new Set(),reboundHidden=new Set();
      for(const target of intent.expanded){const n=await locate(target);if(!n||!owned())continue;reboundExpanded.add(n.id);
        const op=n.kind==='repository'?'overview':'children',root=n.kind==='repository'?null:{kind:'visual',id:n.id};
        const q={schema:MAP_SCHEMA,operation:op,root,expected_authority:data.authority,limits:readLimits()},page=staged.pageFor(q);
        if(reads<settings.autoReads&&!page?.complete)await read(op,root,Boolean(page?.next_cursor));
      }
      for(const target of intent.hidden){const n=await locate(target);if(n)reboundHidden.add(n.id);}
      const reboundContexts=new Set();for(const target of intent.contexts){const n=await locate(target);if(n){reboundContexts.add(n.id);if(reads<settings.autoReads)await read('neighbors',{kind:'visual',id:n.id});}}
      const chosen=await locate(intent.selection);
      if(!owned()||data.status!=='ready')return false;
      // Receipt cache swaps only here. Geometry/camera intent is separate and
      // may warm-start via remapped positions, never via old evidence proofs.
      const oldPositions=scene?.positions?.()||[],newByIntent=new Map(data.nodes.map(n=>[intentKey(nodeIntent(n.id,data)),n.id]));
      const seeds=oldPositions.flatMap(p=>{const n=nodeIntent(p.id),id=n&&newByIntent.get(intentKey(n));return id?[{...p,id}]:[];});
      suppressModel=true;old.dispose();suppressModel=false;model=staged;retainedIntent=intent;staging=null;expanded.clear();for(const id of reboundExpanded)expanded.add(id);hiddenChildren.clear();for(const id of reboundHidden)hiddenChildren.add(id);contexts.clear();for(const id of reboundContexts)contexts.add(id);
      anchor=data.nodes.find(n=>n.kind==='repository')?.id||data.root;if(anchor)expanded.add(anchor);selected=chosen?.id||null;desiredSelected=null;mapData=data;rebindRetainedIntent();query=null;sceneKey=null;
      scene?.seedPositions?.(seeds);expansionNotice=reads>=settings.autoReads?'Previous context restored within the automatic read budget; some branches/connections may require explicit expansion or continuation.':'Previous context rebound; missing or changed declarations are not substituted.';updateScene();paint();return true;
    }finally{if(staging===staged){staged.dispose();staging=null;}if(ticket===refreshSerial){refreshing=false;paint();}}
  }
  async function retryMapRead(){mapFailures=0;nextMapAttempt=0;loadedKey=codeMapIndexKey(index);const ok=mapData.authority?await refreshPublishedGraph(loadedKey):await initialExpansion();if(!ok){loadedKey=null;mapFailures++;nextMapAttempt=Date.now()+2000;}return ok;}
  function stopRequestedFocus(){focusSerial++;focusPath='';focusMessage='';}
  function selectWorkspace(id,{label:nextLabel=id}={}){if(!current())return;stopRequestedFocus();folderSerial++;workspace=id;label=nextLabel;workspaceName.textContent=label;setWorkspace(id,label);settingsEnvelope=normalizeGraphSettings(options.readGraphSettings?.());applySettings();showView('map');startIndex();}
  async function focusRequestedPath(initialOverview=null){
    if(!current()||!focusPath||mapData.status!=='ready')return;
    const key=codeMapKey(mapData.authority);if(key===focusKey)return;focusKey=key;
    const expansionTicket=expansionSerial,ticket=++focusSerial,owned=()=>current()&&ticket===focusSerial&&codeMapKey(mapData.authority)===key;
    const result=await resolveCodeMapPath(focusPath,{read:(op,root,more)=>loadMap(op,{root,more,expansionTicket}),snapshot:()=>model.snapshot(),isCurrent:owned,initialOverview});
    if(!owned())return;
    if(result.state==='found'){select(result.node.id,{requested:true});focusMessage=mapData.authority?.variant==='structure_preview'?'Selected saved source in folder structure; evidence is not published.':'Selected saved/on-disk source.';}
    else focusMessage=['limited','label_limit'].includes(result.state)?'The selected path exceeds the map navigation or label limit. No substitute was selected.':'The exact selected path is not available in this map. No substitute was selected.';
    status.textContent=focusMessage;
  }
  function exploration(){return {anchor,expanded,contexts,hiddenChildren,relations};}
  function visibleMap(){
    const data=codeMapVisible(mapData,exploration()),byId=new Map(data.nodes.map(n=>[n.id,n]));
    let fileCount=0,symbolCount=0;
    let allowed=data.nodes.filter(n=>{if(n.kind==='file')return ++fileCount<=settings.maxFiles;if(n.kind!=='symbol')return true;
      if(settings.nodeScope==='files')return false;const kind=n.symbol_kind||'';
      if(/function|method/.test(kind)&&!settings.showFunctions||/class|interface|type/.test(kind)&&!settings.showClasses||!/function|method|class|interface|type/.test(kind)&&!settings.showOtherSymbols)return false;
      return ++symbolCount<=settings.maxSymbols;});
    if(settings.nodeScope==='symbols'){const keep=new Set(data.nodes.filter(n=>n.kind==='symbol').map(n=>n.id));for(const id of [...keep]){let p=byId.get(id)?.parent;const seen=new Set();while(p&&byId.has(p)&&!seen.has(p)){seen.add(p);keep.add(p);p=byId.get(p).parent;}}allowed=allowed.filter(n=>keep.has(n.id)||n.kind==='repository');}
    if(retainedIntent.hidden.length)allowed=allowed.filter(n=>{const intent=nodeIntent(n.id);return intent&&!retainedIntent.hidden.some(h=>h&&h.kind!=='symbol'&&h.path.length<intent.path.length&&h.path.every((part,i)=>part===intent.path[i]));});
    const pins=new Set([selected,anchor]);allowed.sort((a,b)=>Number(pins.has(b.id))-Number(pins.has(a.id)));allowed=allowed.slice(0,settings.maxNodes);
    const ids=new Set(allowed.map(n=>n.id));let relevant=data.edges.filter(e=>ids.has(e.from)&&ids.has(e.to));
    if(selected&&settings.direction!=='both')relevant=relevant.filter(e=>e.relation==='containment'||(settings.direction==='incoming'?e.to===selected:e.from===selected));
    let connections=0;relevant=relevant.filter(e=>e.relation==='containment'||!selected||e.from!==selected&&e.to!==selected||++connections<=settings.connections).slice(0,settings.maxEdges);
    return {...data,nodes:allowed,edges:relevant,displayOmitted:data.nodes.length-allowed.length,edgeOmitted:data.edges.length-relevant.length};
  }
  function updateScene(){
    if(!scene)return;
    if(!mapData.authority){scene.clear();sceneKey=null;return;}
    if(!['ready','loading','error'].includes(mapData.status))return;
    const visible=visibleMap(),key=codeMapKey({authority:visible.authority,nodes:visible.nodes.map(n=>n.id),edges:visible.edges.map(e=>e.id)});
    if(key!==sceneKey){
      const dto={authority:visible.authority,status:'ready',nodes:visible.nodes.map(({id,label,kind,parent,symbol_kind})=>({id,label,kind,parent,...(symbol_kind?{symbol_kind}:{})})),edges:visible.edges.map(({from,to,relation,state})=>({from,to,relation,state})),retainPresentation:settings.preserveCamera&&sceneAuthorityKey!==null};
      const fits=()=>new TextEncoder().encode(JSON.stringify(dto)).length<=4*1024*1024;
      while(!fits()&&dto.edges.length)dto.edges.length=Math.floor(dto.edges.length*.75);
      while(!fits()&&dto.nodes.length>1){dto.nodes.length=Math.max(1,Math.floor(dto.nodes.length*.75));const ids=new Set(dto.nodes.map(n=>n.id));dto.edges=dto.edges.filter(e=>ids.has(e.from)&&ids.has(e.to));}
      sceneByteNotice=dto.nodes.length<visible.nodes.length||dto.edges.length<visible.edges.length?`Scene byte limit: ${visible.nodes.length-dto.nodes.length} nodes / ${visible.edges.length-dto.edges.length} edges omitted; retained evidence unchanged.`:'';
      try{scene.update(dto);sceneKey=key;}catch{sceneKey=null;sceneByteNotice='Scene budget unavailable; reduce displayed nodes or edges.';}
    }
    sceneAuthorityKey=codeMapKey(mapData.authority);scene.select(selected);revealSelection();
  }
  function branchRequest(id,operation='children'){
    return {schema:MAP_SCHEMA,operation: id===anchor&&operation==='children'?'overview':operation,
      root:id===anchor&&operation==='children'?null:{kind:'visual',id},direction:operation==='neighbors'?connectionDirection:'both',expected_authority:copy(mapData.authority),limits:readLimits()};
  }
  function loadMap(operation,{root=null,more=false,expansionTicket=null,direction=operation==='neighbors'?connectionDirection:'both'}={}){
    const atEpoch=epoch,workspaceId=workspace;
    const task=async()=>{
      if(expansionTicket!==null&&expansionTicket!==expansionSerial)return false;
      if(!current()||!hostVisible||view!=='map'||atEpoch!==epoch||workspaceId!==workspace||!codeMapMayRead(index))return false;
      actionSerial++;actionAborter.abort();actionAborter=new AbortController();
      const owned={schema:MAP_SCHEMA,operation,root,direction,expected_authority:copy(mapData.authority),limits:readLimits()};
      query=owned;const result=await model.run(workspaceId,owned,{more});
      if(result&&operation==='children'&&root&&(expansionTicket===null||expansionTicket===expansionSerial))expanded.add(root.id);
      if(result&&operation==='neighbors'&&root&&(expansionTicket===null||expansionTicket===expansionSerial))contexts.add(root.id);
      if(result){updateScene();paint();}return result;
    };
    const pending=readQueue.then(task,task);readQueue=pending.catch(()=>false);return pending;
  }
  async function initialExpansion({preserve=false}={}){
    const ticket=++expansionSerial;if(!preserve){retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};expanded.clear();contexts.clear();hiddenChildren.clear();anchor=null;}expansionNotice='';
    if(!await loadMap('overview',{expansionTicket:ticket})||ticket!==expansionSerial)return false;
    return expandLevels(settings.initialDepth,{ticket,fromRoot:true,preserve});
  }
  async function expandBranch(id,{more=false}={}){
    const ticket=++expansionSerial;expansionBusy=false;stopRequestedFocus();const node=mapData.nodes?.find(n=>n.id===id);if(!node||!expandable(node))return false;
    retainedIntent.hidden=retainedIntent.hidden.filter(n=>intentKey(n)!==intentKey(nodeIntent(id)));hiddenChildren.delete(id);expanded.add(id);const r=branchRequest(id);const page=model.pageFor(r);
    if(!more&&page?.complete){updateScene();paint();return true;}
    const result=await loadMap(r.operation,{root:r.root,more,expansionTicket:ticket});updateScene();paint();return result;
  }
  function collapseBranch(id){
    const node=mapData.nodes?.find(n=>n.id===id);if(!node||!expandable(node))return;
    stopRequestedFocus();expansionSerial++;expansionBusy=false;hiddenChildren.add(id);const descriptor=nodeIntent(id);if(descriptor&&!retainedIntent.hidden.some(n=>intentKey(n)===intentKey(descriptor)))retainedIntent.hidden.push(descriptor);retainedIntent.expanded=retainedIntent.expanded.filter(n=>{const parent=nodeIntent(id);return intentKey(n)!==intentKey(parent)&&!(parent?.kind!=='symbol'&&parent.path.every((part,i)=>n.path[i]===part));});expanded.delete(id);const byId=new Map((mapData.nodes||[]).map(n=>[n.id,n]));
    for(const key of [...expanded,...contexts]){let at=byId.get(key),seen=new Set();while(at?.parent&&!seen.has(at.id)){seen.add(at.id);if(at.parent===id){expanded.delete(key);contexts.delete(key);break;}at=byId.get(at.parent);}}
    contexts.delete(id);updateScene();paint();
  }
  function childrenShown(id,visible=null,byId=null){
    if(hiddenChildren.has(id))return false;
    if(expanded.has(id))return true;
    byId??=new Map((mapData.nodes||[]).map(n=>[n.id,n]));
    return (visible||visibleMap()).nodes.some(n=>descendantOf(n.id,id,byId));
  }
  function toggleBranchChildren(id){
    const node=mapData.nodes?.find(n=>n.id===id);if(!current()||!node||!expandable(node))return;
    if(childrenShown(id))collapseBranch(id);else if(mapData.status==='ready')return expandBranch(id);
  }
  async function expandLevels(levels=1,{ticket=++expansionSerial,fromRoot=false,preserve=false}={}){
    if(!fromRoot)stopRequestedFocus();expansionBusy=true;expansionNotice='Expanding existing folder data…';paint();
    let reads=0,limited=false;const seen=new Set();
    const starts=fromRoot?[anchor]:visibleMap().nodes.filter(n=>expandable(n)&&!expanded.has(n.id)).map(n=>n.id);
    const stack=starts.slice().reverse().map(id=>({id,depth:0}));
    try{while(stack.length){
      if(ticket!==expansionSerial||!current()||!hostVisible||view!=='map')return false;
      const {id,depth}=stack.pop();if(!id||seen.has(id)||hiddenChildren.has(id)||depth>=levels||preserve&&!expanded.has(id))continue;seen.add(id);
      if(reads>=settings.autoReads||mapData.nodes.length>=Math.min(settings.autoNodes,settings.retainedNodes)){limited=true;break;}
      expanded.add(id);const r=branchRequest(id),page=model.pageFor(r);
      if(!page?.complete){if(!await loadMap(r.operation,{root:r.root,more:Boolean(page?.next_cursor),expansionTicket:ticket}))return false;reads++;}
      if(ticket!==expansionSerial)return false;
      if(model.pageFor(r)?.next_cursor)limited=true;
      // Depth-first interleaving reserves useful descendant reads before a wide
      // root can consume the entire budget on immediate sibling folders.
      const children=mapData.nodes.filter(n=>n.parent===id&&expandable(n))
        .sort((a,b)=>b.source_files-a.source_files||a.label.localeCompare(b.label));
      for(const child of children.slice().reverse())stack.push({id:child.id,depth:depth+1});
      updateScene();paint();
    }
    if(ticket===expansionSerial)expansionNotice=limited?'Some branches have more data. Expand a branch or use Load more.':'Expanded folder context; choose a branch to explore further.';
    return ticket===expansionSerial;
    }finally{if(ticket===expansionSerial){expansionBusy=false;updateScene();if(fromRoot&&scene?.snapshot().cameraMode==='default')scene.fit();paint();}}
  }
  async function connect(id){if(mapData.authority?.variant!=='published')return false;const ticket=++expansionSerial;expansionBusy=false;return loadMap('neighbors',{root:{kind:'visual',id},expansionTicket:ticket});}
  function loadBranchMore(){const ticket=++expansionSerial;expansionBusy=false;const node=mapData.nodes.find(n=>n.id===selected),id=node?.id||anchor;if(!id)return false;const r=branchRequest(id,['file','symbol'].includes(node?.kind)?'neighbors':'children');return loadMap(r.operation,{root:r.root,more:true,expansionTicket:ticket,direction:r.direction});}
  function select(id,{requested=false}={}){
    if(!current())return;const node=mapData.nodes?.find(n=>n.id===id);if(!node)return;
    if(!requested){retainedIntent.selection=nodeIntent(id);focusSerial++;focusPath='';focusMessage='';}
    expansionSerial++;expansionBusy=false;selected=id;actionSerial++;actionAborter.abort();actionAborter=new AbortController();scene.select(id);togglePanel('inspector',true);paintList();paintSelection();
    if(['file','symbol'].includes(node.kind)&&mapData.authority?.variant==='published')void connect(id);
  }
  function showView(nextView){
    if(nextView!==view){expansionSerial++;expansionBusy=false;}view=nextView;mapPanel.hidden=view!=='map';evidencePanel.hidden=view!=='evidence';mapButton.setAttribute('aria-pressed',String(view==='map'));evidenceButton.setAttribute('aria-pressed',String(view==='evidence'));
    syncVisibility();
    if(view==='evidence')ensureEvidence();
  }
  function syncVisibility(){release?.setVisible?.(hostVisible&&view==='map'&&!document.hidden);if(scene){if(hostVisible&&view==='map'){scene.resume();resize();}else scene.suspend();}}
  function setVisible(value){if(disposed||hostVisible===(value===true))return;hostVisible=value===true;if(!hostVisible){expansionSerial++;expansionBusy=false;}release?.setVisible?.(hostVisible&&view==='map'&&!document.hidden);syncVisibility();}
  function ensureEvidence(){
    if(legacy||!mountEvidence)return legacy;
    const panel=el('div');evidencePanel.append(panel);
    legacy=mountEvidence({...options,element:panel,requestMap:null,initialQuery:false,menuIntegrated:true,workspaceId:workspace,workspaceLabel:label,
      isCurrent:current,readGraphSettings:()=>settings,onNavigateResource:resource=>focusResource(resource),setWorkspace:(id,nextLabel)=>selectWorkspace(id,{label:nextLabel}),
      subscribeIndex:(_id,{onStatus})=>{subscribers.add(onStatus);queueMicrotask(()=>{if(current()&&subscribers.has(onStatus))onStatus(copy(index));});
        const off=()=>subscribers.delete(onStatus);off.refresh=()=>release?.refresh?.();off.readmit=()=>release?.readmit?.();return off;}});
    return legacy;
  }
  async function evidence(operation,extra={}){
    if(!current()||!workspace||['denied','erased'].includes(index.lifecycle))return false;
    showView('evidence');const active=ensureEvidence();await Promise.resolve();
    return current()&&legacy===active?active?.run(operation,extra):false;
  }
  function togglePanel(which,open){
    const panel=which==='explorer'?explorer:inspector,toggle=which==='explorer'?exploreToggle:inspectToggle;
    const hadFocus=panel.contains(document.activeElement);
    panel.hidden=open===undefined?!panel.hidden:!open;toggle.setAttribute('aria-expanded',String(!panel.hidden));
    if(panel.hidden&&hadFocus)presentationSelect.focus();
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
    const visible=visibleMap(),byId=new Map(visible.nodes.map(n=>[n.id,n])),children=new Map();
    const order=(a,b)=>(a.kind==='file')-(b.kind==='file')||a.label.localeCompare(b.label)||a.id.localeCompare(b.id);
    for(const n of visible.nodes){if(!children.has(n.parent))children.set(n.parent,[]);children.get(n.parent).push(n);}
    const roots=visible.nodes.filter(n=>!byId.has(n.parent)).sort(order),rows=[],seen=new Set(),stack=roots.slice().reverse().map(node=>({node,depth:0}));
    while(stack.length){const {node,depth}=stack.pop();if(seen.has(node.id))continue;seen.add(node.id);rows.push({...node,displayDepth:depth});
      for(const child of (children.get(node.id)||[]).sort(order).slice().reverse())stack.push({node:child,depth:Math.min(64,depth+1)});}
    listPage=Math.min(listPage,Math.max(0,Math.ceil(rows.length/settings.explorerRows)-1));const start=listPage*settings.explorerRows;
    list.replaceChildren(...rows.slice(start,start+settings.explorerRows).map((n,i)=>button(`${!expandable(n)?'':childrenShown(n.id,visible,byId)?'▾ ':'▸ '}${n.label} · ${n.kind}`,()=>{select(n.id);if(expandable(n))void toggleBranchChildren(n.id);},{'data-code-map-id':n.id,role:'treeitem','aria-selected':String(n.id===selected),'aria-level':n.displayDepth+1,...(expandable(n)?{'aria-expanded':String(childrenShown(n.id,visible,byId))}:{}),style:`padding-inline-start:${8+Math.min(12,n.displayDepth)*14}px`,title:n.label,tabindex:i===0?0:-1})));
    if(focused){const items=[...list.querySelectorAll('[data-code-map-id]')],target=items.find(n=>n.dataset.codeMapId===focused);
      if(target){for(const item of items)item.tabIndex=-1;target.tabIndex=0;target.focus();}}
    listLabel.textContent=`${rows.length?start+1:0}–${Math.min(rows.length,start+settings.explorerRows)} of ${rows.length} loaded`;
    previous.disabled=listPage===0;next.disabled=start+settings.explorerRows>=rows.length;const at=mapData.nodes.find(n=>n.id===selected),r=anchor?branchRequest(at?.id||anchor,['file','symbol'].includes(at?.kind)?'neighbors':'children'):null;more.disabled=mapData.status!=='ready'||!r||!model.pageFor(r)?.next_cursor;
  }
  function paintSelection(){
    const node=mapData.nodes?.find(n=>n.id===selected),selection=model.bindingFor(selected);
    inspectorTitle.textContent=node?.label||'Select a folder, file or relationship';
    inspectorSummary.textContent=node?`${node.symbol_kind||node.kind}${node.qualified_name?' · '+node.qualified_name:''} · ${node.source_files} source files · ${node.asset_files} assets${mapData.authority?.variant==='structure_preview'?' · structure only; not evidence':''}`:'';
    const showing=Boolean(node&&childrenShown(node.id));drill.textContent=showing?'Hide children':'Show children';drill.setAttribute('aria-expanded',String(showing));drill.hidden=!expandable(node);drill.disabled=!node||(!showing&&mapData.status!=='ready');neighbors.disabled=!node||mapData.status!=='ready'||mapData.authority?.variant!=='published';
    const canonical=selection?.visual_id===selected&&mapData.authority?.variant==='published'&&mapData.status==='ready'&&indexPresentation(index).usable;
    inspectEvidence.disabled=!canonical;
    for(const control of evidenceActions)control.disabled=!canonical||!capability.available||!onEvidenceAction;
    const edges=(mapData.edges||[]).filter(e=>e.relation!=='containment'&&(!selected||e.from===selected||e.to===selected));
    edgeList.replaceChildren(el('strong',{text:`Relationships (${edges.length} loaded)`}),...edges.slice(0,settings.inspectorRows).map(e=>button(`${e.relation} · ${e.state} · ${e.count}${mapData.nodes.some(n=>(n.id===e.from||n.id===e.to)&&n.kind!=='file')?' · folder aggregate':''}`,()=>void model.selectAggregate(e.selection_id))));
    if(edges.length>settings.inspectorRows)edgeList.append(el('span',{text:`${edges.length-settings.inspectorRows} more loaded relationships are outside this inspector.`}));
    const aggregate=mapData.aggregate&&edges.some(e=>e.selection_id===mapData.aggregate.selection_id)?mapData.aggregate:null;referenceList.replaceChildren();
    if(aggregate){referenceList.append(el('strong',{text:`Underlying references (${aggregate.references.length}${aggregate.complete?'':', more remain'})`}),
      ...aggregate.references.map(r=>button(`${r.relation} · ${r.state} · inspect occurrence`,()=>evidence('neighbors',{seed_ids:[r.occurrence_id],direction:'both'}))));
      if(aggregate.next_cursor&&!aggregate.clientLimited)referenceList.append(button('More references',()=>void model.selectAggregate(aggregate.selection_id,{cursor:aggregate.next_cursor})));
    }
    const path=[],seen=new Set();let at=mapData.nodes?.find(n=>n.id===(selected||anchor));
    while(at&&!seen.has(at.id)&&path.length<64){path.unshift(at);seen.add(at.id);at=mapData.nodes.find(n=>n.id===at.parent);}
    breadcrumbs.replaceChildren(...path.map(n=>button(n.label,()=>{stopRequestedFocus();select(n.id);return n.kind==='file'?connect(n.id):expandBranch(n.id);} )));
  }
  function paint(){
    if(!current())return;status.textContent=[refreshing?'Verifying successor; previous published view remains visible.':mapData.authority?.variant==='published'&&index.head_generation&&mapData.authority.receipt.head_generation!==index.head_generation?'Previous published view; successor could not yet be read. Retry map read.':codeMapStatus(index,mapData),expansionNotice,mapData.evicted?'Exploration cache limited; revisit branches to reload.':'',`${visibleMap().nodes.length} displayed / ${mapData.nodes.length} loaded nodes · ${visibleMap().edges.length} displayed edges · effective total cap ${Math.min(settings.maxNodes,settings.retainedNodes)} (files ≤${Math.min(settings.maxFiles,settings.maxNodes,settings.retainedNodes)}, symbols ≤${Math.min(settings.maxSymbols,settings.maxNodes,settings.retainedNodes)})`,index.verified_at?'Last verified '+new Date(index.verified_at).toLocaleTimeString():'',focusMessage,sceneByteNotice,options.savedSourceNotice?'Graph represents saved/on-disk source; unsaved Editor changes were not saved or indexed.':''].filter(Boolean).join(' ');options.onStatus?.(status.textContent);status.dataset.recovering=String(index.recovering===true);
    const usable=indexPresentation(index).usable;search.disabled=searchButton.disabled=evidenceButton.disabled=!workspace||['denied','erased'].includes(index.lifecycle);
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
    const selection=model.bindingFor(selected);if(!current()||mapData.status!=='ready'||!selection||selection.visual_id!==selected||!capability.available||!indexPresentation(index).usable)return;
    const receipt=copy(selection.receipt),id=selection.resource_id,workspaceId=workspace,authority=codeMapKey(mapData.authority),ticket=++actionSerial;
    const valid=()=>current()&&ticket===actionSerial&&workspace===workspaceId&&model.bindingFor(selected)?.resource_id===id&&codeMapKey(mapData.authority)===authority&&indexPresentation(index).usable;
    for(const b of evidenceActions)b.disabled=true;
    try{const result=await onEvidenceAction?.(kind,{node:{id,kind:id.startsWith('file:')?'file':'declaration'},receipt,workspaceId,isCurrent:valid,signal:actionAborter.signal});if(valid()){status.textContent=result?.message||'Source action completed.';if(result?.ok&&kind==='open')scene.activity?.([selected]);}}
    catch{if(valid())status.textContent='Current source action unavailable.';}
    finally{if(valid())paintSelection();}
  }
  listen(search,'keydown',event=>{if(event.key==='Enter'){
    event.preventDefault();
    void evidence('search',{query:search.value||undefined});
  }});
  listen(list,'keydown',event=>{
    const items=[...list.querySelectorAll('[data-code-map-id]')],at=items.indexOf(document.activeElement),last=items.length-1;
    const next=({ArrowDown:Math.min(last,at+1),ArrowUp:Math.max(0,at-1),Home:0,End:last})[event.key];
    if(next===undefined||!items[next])return;event.preventDefault();for(const item of items)item.tabIndex=-1;items[next].tabIndex=0;items[next].focus();
  });
  listen(element,'keydown',event=>{if(event.key==='Escape'&&(!explorer.hidden||!inspector.hidden)){
    event.stopPropagation();presentationSelect.focus();
    togglePanel('explorer',false);togglePanel('inspector',false);}});
  listen(preference,'change',async()=>{const ticket=++preferenceSerial,value=preference.checked;preference.disabled=true;
    try{const saved=await writePreference?.(value);if(current()&&ticket===preferenceSerial){preference.checked=saved===true;preferenceText.textContent=saved===true?'Automatic context enabled.':'Automatic context is off.';}}
    catch{if(current()&&ticket===preferenceSerial){preference.checked=false;preferenceText.textContent='Setting unavailable; automatic context is off.';}}
    finally{if(current()&&ticket===preferenceSerial)preference.disabled=!writePreference;}});
  const prefTicket=++preferenceSerial;Promise.resolve().then(()=>readPreference?.()).then(value=>{if(current()&&prefTicket===preferenceSerial){preference.checked=value===true;preference.disabled=!writePreference;preferenceText.textContent=value===true?'Automatic context enabled.':'Automatic context is off.';}},()=>{});
  function menuCapture(){return {workspace,epoch,view,presentation,layoutPaused,selected,connectionDirection,relations:[...relations].sort().join(','),authority:codeMapKey(mapData.authority||null),root:mapData.root||null,
    page:mapData.page?.next_cursor||null,automaticContext:preference.checked,preferencePending:preference.disabled,status:mapData.status||'idle',index:codeMapIndexKey(index),expansion:expansionSerial,childrenShown:selected?childrenShown(selected):false,
    evidence:view==='evidence'&&legacy?JSON.stringify(legacy.menuCapture?.()||null):null};}
  // Loading/progress changes invalidate captured evidence, not harmless
  // presentation commands. Both paths still bind the same mounted folder/epoch.
  function menuScopeCurrent(c){return current()&&c&&c.workspace===workspace&&c.epoch===epoch;}
  function menuIsCurrent(c){return menuScopeCurrent(c)&&codeMapKey(c)===codeMapKey(menuCapture());}

  function menuCommands(c){
    if(!menuIsCurrent(c))return [];
    const presentationCommands=new Set(['choose','map','evidence','explorer','inspector','fit','zoom-in','zoom-out','settings','layout-pause','settle','diagnostics']);
    const cmd=(id,label,menu,run,disabledReason='',checked)=>({id:'graph.'+id,label,menu,run,disabledReason,
      current:()=>presentationCommands.has(id)||id.startsWith('relation-')
        ?menuScopeCurrent(c)&&c.view===view&&c.presentation===presentation
        :menuIsCurrent(c),...(typeof checked==='boolean'?{checked}:{})});
    const control=(id,label,menu,b)=>cmd(id,label,menu,()=>b.click(),b.disabled?'Unavailable for the current selection or index.':'');
    const node=mapData.nodes.find(n=>n.id===selected),mapView=view==='map';
    const result=[control('choose','Choose code folder…','File',folder),
      cmd('map','Graph canvas','View',()=>showView('map'),'',mapView),
      {...control('evidence','Search & evidence','View',evidenceButton),checked:!mapView},
      cmd('explorer','Explorer','View',()=>togglePanel('explorer'),!mapView?'Show the graph canvas first.':'',!explorer.hidden),
      cmd('inspector','Inspector','View',()=>togglePanel('inspector'),!mapView?'Show the graph canvas first.':'',!inspector.hidden),
      control('overview','Overview and useful depth','View',overview),
      cmd('fit','Fit visible graph','View',()=>{scene.fit();revealSelection();},!mapView?'Show the graph canvas first.':''),
      cmd('zoom-in','Zoom in','View',()=>scene.zoom(1.2),!mapView?'Show the graph canvas first.':''),
      cmd('zoom-out','Zoom out','View',()=>scene.zoom(1/1.2),!mapView?'Show the graph canvas first.':''),
      cmd('expand','Expand selected branch','Graph',()=>expandBranch(selected),!mapView||!node||!expandable(node)||expansionBusy||mapData.status!=='ready'?'Select a folder in the canvas or Explorer.':''),
      cmd('collapse','Collapse selected branch','Graph',()=>collapseBranch(selected),!mapView||!node||!expandable(node)||!childrenShown(selected)?'Select an expanded branch.':''),
      cmd('deeper','Expand next level','Graph',()=>expandLevels(settings.expansionDepth),!mapView||expansionBusy||!anchor?'No current expandable graph.':''),
      cmd('more','Load more for selected branch','Graph',()=>loadBranchMore(),more.disabled?'This branch has no available continuation.':''),
      ...(!mapView?[]:[control('connections','Connections for selected item','Graph',neighbors)]),
      ...(!mapView?[]:[control('inspect','Inspect selected evidence','Graph',inspectEvidence)]),
      ...(!mapView?[]:[control('open','Open selected source','File',evidenceActions[0])]),
      ...(!mapView?[]:[control('add','Add selected evidence to chat','Graph',evidenceActions[1])]),
      ...(!mapView?[]:[control('ask','Ask about selected evidence','Graph',evidenceActions[2])]),
      control('recheck','Refresh now','Graph',recheck),
      ...(!retry.hidden?[control('retry','Retry indexing','Graph',retry)]:[]),
      ...(!readmit.hidden?[control('readmit','Readmit erased index','Graph',readmit)]:[]),
      control('observe-stop','Stop observing','Graph',detach),
      cmd('auto-context','Automatic code context','Graph',()=>{preference.checked=!preference.checked;preference.dispatchEvent(new Event('change'));},preference.disabled?'Preference unavailable.':'',preference.checked),
      cmd('settings','Graph settings…','View',()=>showSettings()),
      cmd('reload','Retry map read','Graph',()=>retryMapRead(),!codeMapMayRead(index)?'No readable scope.':''),
      cmd('layout-pause',layoutPaused?'Resume layout':'Pause layout','Graph',()=>{layoutPaused=!layoutPaused;scene.setLayoutPaused?.(layoutPaused);},!mapView||presentation==='map'?'Choose Web or Gravity.':'',layoutPaused),
      cmd('settle','Reheat and settle layout','Graph',()=>scene.relayout?.(),!mapView||presentation==='map'?'Choose Web or Gravity.':''),
      cmd('diagnostics','Coverage and diagnostics','View',()=>{diagnosticPanel.hidden=!diagnosticPanel.hidden;},'',!diagnosticPanel.hidden)];
    for(const direction of ['both','incoming','outgoing'])result.push(cmd('direction-'+direction,'Connections: '+direction,'Graph',()=>{connectionDirection=direction;if(selected)void connect(selected);},!mapView||!selected||mapData.authority?.variant!=='published'?'Select published code evidence.':'',connectionDirection===direction));
    for(const relation of MAP_RELATIONS)result.push(cmd('relation-'+relation,'Show '+relation+' relationships','View',()=>{relations.has(relation)?relations.delete(relation):relations.add(relation);updateScene();paint();},!mapView?'Show the graph canvas first.':'',relations.has(relation)));
    if(view==='evidence'&&legacy)result.push(...(legacy.menuCommands?.(JSON.parse(c.evidence))||[]).map(descriptor=>({...descriptor,current:()=>menuIsCurrent(c)&&legacy?.menuIsCurrent?.(JSON.parse(c.evidence))})));
    return result;
  }
  presentation=settingsEnvelope.presentation||'map';settings=resolveGraphSettings(settingsEnvelope,presentation);presentationSelect.value=presentation;scene.setPresentation?.(presentation);applySettings();resize();paint();if(workspace)startIndex();
  function dispose(){if(disposed)return;disposed=true;expansionSerial++;indexSerial++;folderSerial++;actionSerial++;preferenceSerial++;release?.();release=null;actionAborter.abort();capabilityAborter.abort();aborter.abort();settingsDispose?.();refreshSerial++;staging?.dispose();model.dispose();legacy?.dispose();scene.dispose();resizeObserver.disconnect();observer.disconnect();themeObserver.disconnect();subscribers.clear();}
  return {run:(operation,extra={})=>operation==='overview'?loadMap('overview'):evidence(operation,extra),selectWorkspace,isCurrent:current,isMounted:()=>!disposed&&element.isConnected,
    openSettings:showSettings,snapshot:()=>view==='evidence'&&legacy?legacy.snapshot():model.snapshot(),camera:()=>scene.snapshot().camera,presentationStats:()=>scene.snapshot(),menuCapture,menuScopeCurrent,menuIsCurrent,menuCommands,setVisible,dispose};
}
