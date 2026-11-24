import { codeGraphExportSnapshot, codeGraphExportJSON, codeGraphExportHTML, codeGraphExportGraphML, codeGraphExportSVG, codeGraphExportCanvas, codeGraphExportCypher, codeGraphExportMarkdown } from './codeGraphExport.js';
// Compact app-only geometry with the existing evidence workbench on demand.
// One index subscriber owns admission/observation; visual reads never index.
import { CODE_GRAPH_DISPLAY, resolveGraphSettings, normalizeGraphSettings, mountGraphSettings } from './graphSettings.js';
import { graphBudgetSummary } from './graphBudgetSummary.js';
import { MAP_RELATIONS, MAP_SCHEMA, codeMapKey, createCodeMapModel, mapRequest, validateMapResult } from './codeMapModel.js';
import { createCodeMapScene } from './codeMapScene.js';
import { createCodeMapProgression } from './codeMapProgression.js';
import { indexPresentation, indexUpdateLabel, indexPhaseLabel } from './codeIndexObserver.js';
import { resolveCodeMapPath } from './codeMapPath.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { installEditorFilesStyles } from './editorFilesBrowser.js';
import { capturedChangeButton } from '../historyView.js';
import { createCodeActivityModel, joinCodeActivityHistory, subscribeToCodeContextBoundaries } from './codeActivityModel.js';
import { codeReceiptKey, createCodeGraphModel } from './codeGraphModel.js';
import { createCodeMapGeometry } from './codeMapGeometry.js';
import { registerAdapter } from '../custom-context-menu.js';

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
  return codeMapKey(Object.fromEntries(['workspace_revision','registration_id','checkout_id','admission_epoch','policy_generation','head_generation','config_fingerprint'].map(k=>[k,index[k]??null])));
}
export function codeMapMayRead(index={}) {
  return ['admitted','queued','building','running','ready','failed','cancelled'].includes(index.lifecycle)
    && (index.lifecycle!=='failed'||Boolean(index.head_generation));
}
// Diagnostic presentation is not the index API. Never copy every source path,
// parser error or scene label into a hidden DOM node on each progress update.
export function codeMapIndexDiagnostics(index={}) {
  const out={},scalar=value=>typeof value==='string'?value.slice(0,256):typeof value==='boolean'||Number.isFinite(value)?value:undefined;
  for(const key of ['workspace_id','workspace_revision','registration_id','checkout_id','admission_epoch','policy_generation','run_id','head_generation','lifecycle','freshness','config_fingerprint','recovering','coverage_complete','budget_omitted_files']){
    const value=scalar(index[key]);if(value!==undefined)out[key]=value;
  }
  out.progress={};for(const key of ['completed_files','total_files','heartbeat_at','phase','phase_completed','phase_total','phase_unit','phase_observed_at']){const value=scalar(index.progress?.[key]);if(value!==undefined)out.progress[key]=value;}
  out.error=index.error?{code:scalar(index.error.code),message:typeof index.error.message==='string'?index.error.message.slice(0,512):undefined}:null;
  out.errorCount=Array.isArray(index.errors)?index.errors.length:null;
  out.coverageSummary=null;
  if(Array.isArray(index.coverage)){
    const known=new Set(['selected','indexed','no_source_units','no_units','parse_failed','binary','ignored','outside_root','oversize','oversized','unsupported','unavailable','permission_denied','budget_omitted']),statuses={};
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
  if(exploration.solo&&byId.has(exploration.solo))ids.add(exploration.solo);
  const childIds=new Map();
  for(const node of nodes){
    if(!expanded.has(node.parent))continue;
    if(!childIds.has(node.parent))childIds.set(node.parent,[]);
    childIds.get(node.parent).push(node.id);
  }
  const queue=[...ids];
  for(let i=0;i<queue.length;i++)for(const child of childIds.get(queue[i])||[]){
    if(!ids.has(child)){ids.add(child);queue.push(child);}
  }
  for(const edge of edges)if(edge.relation!=='containment'&&relations.has(edge.relation)&&(contexts.has(edge.from)||contexts.has(edge.to))){ids.add(edge.from);ids.add(edge.to);}
  for(const id of [...ids]){let parent=byId.get(id)?.parent;const seen=new Set();while(parent&&byId.has(parent)&&!seen.has(parent)&&seen.size<64){seen.add(parent);ids.add(parent);parent=byId.get(parent).parent;}}
  // A hidden subtree wins over outside relationship contexts and late cache
  // merges. With a mask active, incomplete/cyclic ancestry cannot prove a
  // context endpoint lies outside it; keep only known paths to the real root.
  if(hidden.size)for(const id of [...ids]){
    let at=id;const seen=new Set();let allowed=false;
    for(let depth=0;depth<=64;depth++){
      if(at===root||at===exploration.solo){allowed=true;break;}
      if(!byId.has(at)||seen.has(at))break;seen.add(at);
      const parent=byId.get(at).parent;if(parent===null||hidden.has(parent))break;at=parent;
    }
    if(!allowed)ids.delete(id);
  }
  const hiddenTerritories=exploration.hiddenTerritories||new Set();
  if(hiddenTerritories.size)for(const id of [...ids]){
    if(hiddenTerritories.has(id)||[...hiddenTerritories].some(group=>descendantOf(id,group,byId)))ids.delete(id);
  }
  const solo=exploration.solo;
  if(solo&&ids.has(solo)){
    for(const id of [...ids])if(id!==solo&&!descendantOf(id,solo,byId)&&!descendantOf(solo,id,byId))ids.delete(id);
  }
  return {...data,root,nodes:nodes.filter(n=>ids.has(n.id)),edges:edges.filter(e=>relations.has(e.relation)&&ids.has(e.from)&&ids.has(e.to))};
}
export function codeMapStatus(index={},map={}) {
  const lifecycle=index.lifecycle||'missing',presentation=indexPresentation(index),pending=presentation.pending;
  const codes={removal_confirmation_required:'A large removal needs review before indexing.',stale_removal_confirmation:'The removal preview changed. Review removals again.',denied:'Access denied. Choose an authorized folder.',erased:'This index was erased. Readmit it explicitly to start again.',stale_cursor:'The code index changed. Reload the map.',reindex_required:'This older index needs an explicit rebuild.',budget_exceeded:'The published map exceeds this read budget. Adjust Graph limits or retry after indexing.',busy:'The index is busy. Retry the map shortly.',cancelled:'Map loading was cancelled.'};
  if(['denied','erased'].includes(lifecycle))return codes[lifecycle];
  if(index.error?.code==='code_root_changed')return 'The directory identity changed. Use File > Open folder to select it again.';
  if(map.status==='error'){
    const message=codes[map.error?.code]||'Code map unavailable. Retry or inspect details.';
    return pending?[presentation.text,map.error?.code==='reindex_required'?'Previous map unavailable; waiting for this index.':message].join(' · '):message;
  }
  if(lifecycle==='unavailable')return index.recovering?'Index connection interrupted; rechecking status.':'Index connection unavailable. Recheck status.';
  if(map.authority?.variant==='structure_preview'){
    const p=index.progress||{};
    if (!pending && index.head_generation) {
      const reason = index.error?.message;
      return ['Folder preview · rebuild the source index to see dependencies', reason].filter(Boolean).join(' · ');
    }
    const lifecycleText=lifecycle==='failed'?'Indexing failed':lifecycle==='cancelled'?'Indexing cancelled':pending?'Indexing':'No compatible published dependency head';
    return ['Folder preview · dependencies not yet published',lifecycleText,indexPhaseLabel(p),indexUpdateLabel(p.heartbeat_at),
      index.freshness?`Freshness: ${index.freshness}`:'',index.error?.message?String(index.error.message):'',map.coverage?.inventory_complete?'':'Partial inventory'].filter(Boolean).join(' · ');
  }
  if(map.status==='ready') {
    if(!map.nodes?.length)return 'No visible code or folders in this scope.';
    if(pending)return ['Previous published index',indexPhaseLabel(index.progress),indexUpdateLabel(index.progress?.heartbeat_at)].filter(Boolean).join(' · ');
    if(lifecycle==='cancelled')return 'Previous index · verification cancelled. Retry indexing to continue.';
    if(lifecycle==='failed')return 'Previous index · rebuild failed. Source actions revalidate current files.';
    const omitted=index.budget_omitted_files;
    if(Number.isSafeInteger(omitted)&&omitted>0)return `Code map · partial coverage · ${omitted.toLocaleString()} source files outside the index budget`;
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
const updateText=(element,text)=>{if(element.textContent!==text)element.textContent=text;};

export function mountCodeMapWorkbench(options) {
  const {element,isCurrent=()=>true,workspaceId='',workspaceLabel='',setWorkspace=()=>{},chooseWorkspace,subscribeIndex,
    requestMap,request,onEvidenceAction,readResourceCapabilities,readPreference,writePreference,mountEvidence,
    sceneFactory=createCodeMapScene}=options;
  let disposed=false,workspace=workspaceId,label=workspaceLabel,epoch=0,indexSerial=0,folderSerial=0,actionSerial=0,preferenceSerial=0;
  let index={lifecycle:'missing'},release=null,loadedKey=null,mapData={status:'idle',authority:null,nodes:[],edges:[]},selected=null,query=null,view='map',listPage=0,hostVisible=true;
  let sceneByteNotice='',legacy=null,scene=null,sceneKey=null,sceneAuthorityKey=null,sceneQuery=null,rendererDiagnostic=null,capability={available:false},capabilityKey=null,capabilityPending=null;
  let loadedNodes=null,loadedAuthority=null;
  const seenLoadedNodes=new Set();
  let actionAborter=new AbortController(),capabilityAborter=new AbortController();
  let anchor=null,connectionDirection='both',presentation='map',expansionSerial=0,readQueue=Promise.resolve(),layoutPaused=false,refreshSerial=0,refreshing=false,mapFailures=0,nextMapAttempt=0,attemptKey=null,staging=null,suppressModel=false,expansionBusy=false,expansionNotice='',projectionFailure=null;
  let retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};
  const expanded=new Set(),contexts=new Set(),hiddenChildren=new Set(),hiddenTerritories=new Set(),relations=new Set(MAP_RELATIONS);
  const automaticExpanded=new Set(),automaticContexts=new Set();
  let projectionStart=null, progression=null, deferProjectionPaint=false;
  let projectionStartedAt=null, projectionStartedKey=null, firstWitnessPaint=null;
  let citationRead=null, citationKey=null, restoredFocusKey=null;
  let taskContributionRead=null, taskContributionKey=null;
  let surfaceView='canvas', tablePage=0, geometryStamp='', evidenceRead=null;
  let projectPage=null,projectAuthority=null,projectTrail=[],projectCursor=null,projectRequest=null,projectError='',projectTotals=null,projectRendered=null,projectRegion=null,projectParents=[],projectAutoAuthority=null;
  let traceStart=null, traceResult=null, activeFlow=null, recordedRead=null, recordedReceipt=null;
  const workspaceGeometry=()=>{
    let storage;
    try{storage=window.localStorage;}catch{ /* Geometry is optional. */ }
    return createCodeMapGeometry(options.accountID,workspace,storage);
  };
  let geometry=workspaceGeometry();
  let settingsEnvelope=normalizeGraphSettings(options.readGraphSettings?.()),settingsDispose=null,desiredSelected=null;
  presentation=settingsEnvelope.presentation;
  let settings=resolveGraphSettings(settingsEnvelope,presentation);
  const expandable=n=>Boolean(n&&(mapData.authority?.variant==='published'||!['file','symbol'].includes(n.kind)));
  let focusPath=options.focusPath||'',focusKey=null,focusSerial=0,focusMessage='';
  const subscribers=new Set(),aborter=new AbortController(),signal=aborter.signal;
  const activityModel=createCodeActivityModel();
  const stopBoundaries=subscribeToCodeContextBoundaries(event=>{
    if(current()&&settings.agentActivity&&activityModel.addBoundary(event,activityContext()))paintActivity();
  });
  let activityScope='everything',followActor=null,actorInspection=null,actorRead=null,changeContext=null,soloRoot=null;
  const actorKey=row=>JSON.stringify([row.engineSessionID,row.actorID,row.executionRevision]);
  const current=()=>!disposed&&isCurrent()&&element.isConnected;
  const listen=(node,name,fn,opts={})=>node.addEventListener(name,fn,{...opts,signal});
  const button=(text,fn,attrs={})=>{const b=el('button',{type:'button',class:'copal-btn',text,...attrs});listen(b,'click',fn);return b;};
  const identity=()=>({workspace,epoch});
  element.classList.add('code-map-workbench');element.dataset.codeGraph='true';
  const workspaceName=el('strong',{'data-code-workspace':true,text:label||workspace||'No code folder selected'});
  const presentationSelect=el('select',{'aria-label':'Code Graph presentation'},...['map','web','gravity'].map(value=>el('option',{value,text:value[0].toUpperCase()+value.slice(1)})));
  listen(presentationSelect,'change',()=>{presentation=presentationSelect.value;settingsEnvelope.presentation=presentation;settingsEnvelope.presentationPreference='explicit';options.writeGraphSettings?.(settingsEnvelope);if(presentation==='map'&&layoutPaused){layoutPaused=false;scene.setLayoutPaused?.(false);}showView('map');applySettings();if(options.settingsHost)showSettings(false);});
  async function openFolder(){
    if(!current())return false;
    const ticket=++folderSerial;
    const ownsChoice=()=>current()&&ticket===folderSerial;
    try {
      const binding=await chooseWorkspace?.(ownsChoice);
      if(!ownsChoice()||!binding?.id)return false;
      selectWorkspace(binding.id,{label:binding.name||binding.path||binding.id});
      return true;
    } catch {
      if(ownsChoice())status.textContent='Code folder unavailable. Choose an authorized folder.';
      return false;
    }
  }
  const search=el('input',{type:'search',maxlength:1024,'aria-label':'Search scoped code',placeholder:'Search code…'});
  const searchButton=button('Search',()=>evidence('search',{query:search.value||undefined}));
  const overview=button('Overview',()=>{stopRequestedFocus();showView('map');return initialExpansion();},{'data-code-operation':'overview'});
  const exploreToggle=button('Explore',()=>togglePanel('explorer'),{'aria-expanded':'false','aria-controls':'code-map-explorer-'+crypto.randomUUID()});
  const inspectToggle=button('Inspector',()=>togglePanel('inspector'),{'aria-expanded':'false','aria-controls':'code-map-inspector-'+crypto.randomUUID()});
  const mapButton=button('Graph',()=>switchSurface('canvas'),{'aria-pressed':'true'});
  const tableButton=button('Table',()=>switchSurface('table'),{'aria-pressed':'false'});
  const projectButton=button('Project overview',()=>switchSurface('project'),{'aria-pressed':'false'});
  const evidenceButton=button('Search & evidence',()=>{showView('evidence');if(indexPresentation(index).usable&&legacy?.snapshot().status==='idle')evidence('catalog');},{'aria-pressed':'false'});
  const status=el('span',{'data-code-index-status':true,role:'status','aria-live':'polite'});
  const budgetText=el('p',{'data-code-budget-summary':true});
  const territorySummary=el('section',{'aria-label':'Indexed territories',hidden:true});
  const budgetDetails=el('details',{},el('summary',{text:'Graph details'}),budgetText,territorySummary);
  listen(budgetDetails,'toggle',()=>{if(budgetDetails.open)paintTerritories(visibleMap());});
  const rendererStatus=el('span',{'data-code-renderer':true,text:'Preparing renderer'});
  const recheck=button('Refresh now',()=>index.head_generation||index.previous_head_generation?release?.verify?.():release?.refresh?.());
  const retry=button('Retry indexing',()=>startIndex({preserve:Boolean(mapData.authority)}));
  let removalReviewSerial=0;
  const removalReview=el('section',{class:'code-map-diagnostics',hidden:true,tabindex:'-1','aria-label':'Review source removals'});
  const reviewRemovals=button('Review source removals',()=>void openRemovalReview(),{hidden:true});
  const readmit=button('Readmit erased index',()=>{if(index.lifecycle==='erased')void release?.readmit?.();});
  const detach=button('Stop observing',()=>{indexSerial++;release?.();release=null;invalidate();index={lifecycle:'unavailable'};notifyEvidence();paint();});
  const preference=el('input',{type:'checkbox','aria-label':'Automatic code context',disabled:true});
  const preferenceText=el('span',{text:'Automatic context is off until verified.'});
  const diagnostics=el('pre',{'data-code-diagnostics':true});
  const settingsPanel=el('section',{class:'code-map-diagnostics',hidden:true,'aria-label':'Graph settings'});
  const exportPanel=el('section',{class:'code-map-diagnostics',hidden:true,'aria-label':'Graph export preview'});
  let renderedProjection=null;
  const diagnosticPanel=el('section',{class:'code-map-diagnostics',hidden:true,'aria-label':'Code Graph coverage and diagnostics'},diagnostics);
  const header=el('header',{class:'code-map-header'},el('span',{class:'code-map-title',text:'Code Graph'}),workspaceName);
  const grouping=el('select',{'aria-label':'Layout grouping'},...[
    ['free','Free'],['folders','Folders'],['workspaces','Workspace']
  ].map(([value,text])=>el('option',{value,text})));
  const groupingLabel=el('label',{},'Grouping ',grouping);
  listen(grouping,'change',()=>{settingsEnvelope.modes[presentation].grouping=grouping.value;options.writeGraphSettings?.(settingsEnvelope);applySettings();});
  const restoreTerritory=button('Restore all territories',restoreTerritories,{hidden:true});
  const activitySelect=el('select',{'aria-label':'Graph activity scope'},...[
    ['everything','Everything'],['chat','This chat'],['changed','Changed']
  ].map(([value,text])=>el('option',{value,text})));
  listen(activitySelect,'change',()=>{
    activityScope=activitySelect.value;
    if(activityScope==='changed'){
      stopFollowing();closeObservedWorker();
      if(currentChange()&&!recordedChangeNode())void focusResource({id:changeContext.resourceID,receipt:mapData.authority.receipt});
    }
    updateScene();paint();
  });
  const enableReads=button('Live reads',()=>{
    settingsEnvelope.modes[presentation].agentActivity=!settings.agentActivity;
    options.writeGraphSettings?.(settingsEnvelope);applySettings();
  },{'aria-pressed':'false'});
  const stopCamera=button('Stop following',()=>stopFollowing(),{hidden:true});
  const viewOptions=el('details',{class:'code-map-options'},el('summary',{text:'View options'}),
    el('div',{class:'code-map-toolbar'},groupingLabel,restoreTerritory,el('label',{},'Activity ',activitySelect),enableReads));
  const toolbar=el('nav',{class:'code-map-toolbar code-map-primary-tools','aria-label':'Code map presentation'},
    mapButton,projectButton,tableButton,evidenceButton,el('label',{},'Layout ',presentationSelect),viewOptions,stopCamera);
  const viewport=el('div',{class:'code-map-viewport','data-code-viewport':true});
  const tableRows=el('tbody'), tableStatus=el('span',{role:'status'});
  const tablePrevious=button('Previous',()=>{tablePage--;paintTable();});
  const tableNext=button('Next',()=>{tablePage++;paintTable();});
  const tablePanel=el('section',{
    class:'code-map-table-panel',hidden:true,'aria-label':'Graph table',tabindex:'-1',
  },el('table',{class:'code-map-table'},el('caption',{text:'Loaded graph items'}),
    el('thead',{},el('tr',{},...['Item','Kind','Loaded relationships'].map(text=>el('th',{scope:'col',text})))),tableRows),
  el('div',{class:'code-map-toolbar'},tablePrevious,tableNext,tableStatus));
  const projectSummary=el('p',{'aria-live':'polite'}),projectRows=el('tbody'),projectStatus=el('p',{role:'status'});
  const projectTop=button('Checkout root',()=>void readProjectPage(null,[],null,[]));
  const projectUp=button('Parent folder',()=>{
    const parent=projectParents.at(-1);
    if(parent)void readProjectPage(parent.cursor,parent.trail,parent.region,projectParents.slice(0,-1));
  });
  const projectStart=button('First page',()=>void readProjectPage(null,[]));
  const projectPrevious=button('Previous',()=>void readProjectPage(projectTrail.at(-1),projectTrail.slice(0,-1)));
  const projectNext=button('Next',()=>void readProjectPage(projectPage?.page.next_cursor,[...projectTrail,projectCursor].slice(-128)));
  const projectPanel=el('section',{class:'code-map-project-panel',hidden:true,'aria-label':'Checkout overview',tabindex:'-1'},
    el('h3',{text:'Checkout overview'}),projectSummary,el('div',{class:'code-map-toolbar'},projectTop,projectUp,projectStart,projectPrevious,projectNext),projectStatus,
    el('table',{class:'code-map-table'},el('caption',{text:'Folders and files'}),
      el('thead',{},el('tr',{},...['Item','Kind','Source files','Assets','Browse'].map(text=>el('th',{scope:'col',text})))),projectRows));
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
  const inspectEvidence=button('Inspect evidence',()=>void inspectSelectedEvidence());
  const evidenceActions=['open','add','ask','history'].map((kind,n)=>button(['Open source','Add to chat','Ask about this','Recorded changes'][n],()=>act(kind)));
  const edgeList=el('div',{class:'code-map-relationships'}),referenceList=el('div',{class:'code-map-references'});
  const pinButton=button('Pin position',togglePin,{'aria-pressed':'false'});
  const traceButton=button('Trace from selection',beginTrace);
  const clearTraceButton=button('Clear trace',()=>clearEvidence());
  const flowButton=button('Function flow',()=>void showFlow());
  const pathPanel=el('section',{'aria-label':'Directed path',tabindex:'-1',hidden:true});
  const flowPanel=el('section',{'aria-label':'Function flow',tabindex:'-1',hidden:true});
  const selectionNotice=el('p',{role:'status'});
  const activityList=el('div',{'data-code-activity':true});
  const actors=el('div',{class:'code-map-toolbar','aria-label':'Observed actors'});
  const recordedPanel=el('section',{'aria-label':'Recorded activity',hidden:true,tabindex:'-1'});
  const recordedButton=button('Recorded activity',()=>void showRecordedActivity());
  const citationsPanel=el('section',{'aria-label':'Document citations',hidden:true,tabindex:'-1'});
  const citationsButton=button('Document citations',()=>void showDocumentEvidence());
  const taskContributionsPanel=el('section',{'aria-label':'Task contributions',hidden:true,tabindex:'-1'});
  const taskContributionsButton=button('Task contributions',()=>void showTaskContributions());
  const workerDetails=el('section',{'aria-label':'Observed worker details',hidden:true});
  const inspector=el('aside',{id:inspectToggle.getAttribute('aria-controls'),class:'code-map-inspector','aria-label':'Selected code map item',hidden:true},
    el('div',{class:'code-map-panel-heading'},inspectorTitle,button('Close inspector',()=>togglePanel('inspector',false))),inspectorSummary,
    el('div',{class:'code-map-toolbar'},drill,neighbors,inspectEvidence,...evidenceActions),
    el('div',{class:'code-map-toolbar'},pinButton,traceButton,clearTraceButton,flowButton,recordedButton,citationsButton,taskContributionsButton),selectionNotice,pathPanel,flowPanel,recordedPanel,citationsPanel,taskContributionsPanel,
    actors,activityList,workerDetails,edgeList,referenceList);
  const mapPanel=el('section',{class:'code-map-panel','aria-label':'Code map'},viewport,projectPanel,tablePanel,empty,explorer,inspector);
  const breadcrumbs=el('nav',{class:'code-map-breadcrumbs','aria-label':'Code folder path'});mapPanel.append(breadcrumbs);
  const evidencePanel=el('section',{class:'code-map-evidence','aria-label':'Code search and detailed evidence',hidden:true});
  const legend=el('span',{text:'Files and symbols · typed static relationships, not execution'});
  const summary=el('span',{'data-code-summary':true,role:'status'});
  budgetDetails.append(status,rendererStatus,legend);
  element.append(header,toolbar,el('div',{class:'code-map-status'},summary,retry,reviewRemovals),mapPanel,evidencePanel,removalReview,diagnosticPanel,settingsPanel,exportPanel,budgetDetails);
  let retryingOverview = null;
  async function requestProjection(id, visual, readSignal, reportTimings) {
    const owner = projectionStart;
    const key = codeMapIndexKey(index);
    const automaticOverview = owner && index.lifecycle === 'ready' && index.head_generation
      && visual.mode === 'map' && visual.request.operation === 'overview' && !visual.request.cursor;
    try {
      return await requestMap(id,visual,readSignal,reportTimings);
    } catch (error) {
      if (!automaticOverview || error?.status !== 504) throw error;
      const retrySignal = AbortSignal.any([owner.signal,readSignal]);
      const owns = () => !retrySignal.aborted && owner === projectionStart && id === workspace
        && key === codeMapIndexKey(index) && canLoadAutomatically();
      if (!owns()) throw error;
      retryingOverview = owner;
      paint();
      try {
        await yieldProgression(retrySignal,1000);
        if (!owns()) throw error;
        return await requestMap(id,visual,retrySignal,reportTimings);
      } finally {
        if (retryingOverview === owner) { retryingOverview = null; paint(); }
      }
    }
  }
  const modelOptions={request:requestProjection,getScope:identity,isCurrent:current,getPinned:()=>[anchor,selected].filter(Boolean)};
  let model=createCodeMapModel({...modelOptions,maxNodes:settings.retainedNodes,maxEdges:settings.retainedEdges,maxBytes:settings.cacheMiB*1048576,changed:value=>{
    if(suppressModel)return;
    if(mapData.authority&&value.authority&&codeMapKey(mapData.authority)!==codeMapKey(value.authority)){desiredSelected=selected;contexts.clear();selected=null;}
    if(!value.authority){anchor=null;expanded.clear();contexts.clear();hiddenChildren.clear();}
    mapData=value;rebindRetainedIntent();
    if(value.status==='ready'){const retained=new Set(value.nodes.map(n=>n.id));/* Expansion intent is not a proof of retained adjacency. */for(const id of contexts)if(!retained.has(id))contexts.delete(id);/* Hidden intent survives partial successor pages; it is bounded by retained-cache capacity. */}
    if(value.status==='ready'&&!anchor){anchor=value.nodes.find(n=>n.kind==='repository')?.id||value.root; if(anchor)expanded.add(anchor);}
    if(desiredSelected&&value.nodes.some(n=>n.id===desiredSelected)){selected=desiredSelected;desiredSelected=null;}
    if(selected&&!value.nodes.some(n=>n.id===selected))selected=null;
    if(!deferProjectionPaint){updateScene();paint();}
  }});
  scene=sceneFactory({settings,mount:viewport,id:'copal-code-map-'+crypto.randomUUID(),
    onCameraTakeover:()=>stopFollowing(),onSelect:selection=>{
    if(!current())return;
    if(selection===null){stopRequestedFocus();const receipt=qualifiedReceipt();if(receipt)geometry.rememberFocus(receipt,null);retainedIntent.selection=null;selected=null;actionSerial++;actionAborter.abort();actionAborter=new AbortController();paintList();paintSelection();}
    else if(selection.authorityNamespace===codeMapKey(mapData.authority))select(selection.id);
  },onChange:value=>{if(current()){
    rememberSceneGeometry(value);
    const paint=value.paint;
    const receipt=qualifiedReceipt();
    if(receipt&&paint?.namespace===codeMapKey(mapData.authority)&&paint.witnessedEdges>0&&
        !firstWitnessPaint&&projectionStartedAt!==null&&paint.acknowledgedAt>=projectionStartedAt){
      firstWitnessPaint={generation:receipt.head_generation,relation:paint.firstRelation,
        fromProjectionRequestMs:Math.round(paint.acknowledgedAt-projectionStartedAt)};
    }
    rendererDiagnostic={status:value.status,requestedBackend:value.requestedBackend,backend:value.backend,
      fallbackReason:value.fallbackReason,disposed:value.disposed,drawn:value.drawn?{...value.drawn}:null,
      density:value.density?{...value.density}:null,renderPolicy:value.renderPolicy?{...value.renderPolicy}:null,
      acknowledgedFrame:paint?{sceneCpuMs:Number(paint.sceneCpuMs.toFixed(2)),backend:paint.backend,witnessedEdges:paint.witnessedEdges}:null,
      firstWitnessPaint};
    if(!diagnosticPanel.hidden)renderDiagnostics();
    const renderer=value.backend==='webgl2'?'WebGL2':value.backend==='canvas2d'?'Canvas2D':'Preparing';
    const requested=value.requestedBackend==='ordinary'?'Ordinary':'Auto';
    const reason=value.fallbackReason==='webgl2-context-lost'?'WebGL2 context lost':
      value.fallbackReason==='webgl2-lease-unavailable'?'WebGL2 slots in use':
      value.fallbackReason&&requested==='Auto'?'WebGL2 unavailable':'';
    const rendererPolicy=value.renderPolicy;
    const frameNote=rendererPolicy ? [
      rendererPolicy.placement==='docked'?'Docked':'Expanded',
      rendererPolicy.effectiveAnimatedFps==='unlocked'?'uncapped':rendererPolicy.effectiveAnimatedFps+' FPS',
    ].join(' · ') : '';
    const layoutNote=value.status==='layout-worker-required'?'Reduce displayed nodes or reopen with layout worker support.':
      value.status==='layout-error'?'Layout unavailable.':'';
    updateText(rendererStatus,[`${requested} → ${renderer}`,reason,frameNote,layoutNote,
      value.density?.mode==='binned'?`${value.density.groupedNodes} items in ${value.density.bins} bins · zoom or use Table`:
      value.density?.mode==='overflow'?`${value.density.visibleNodes}/${value.density.totalNodes} visible`:''].filter(Boolean).join(' · '));
  }}});
  const applyPalette=()=>{if(current()&&typeof getComputedStyle==='function')scene.setPalette?.(codeMapThemePalette(getComputedStyle(element)));};
  const themeObserver=new MutationObserver(applyPalette);
  for(const target of [document.documentElement,document.body])if(target)themeObserver.observe(target,{attributes:true,attributeFilter:['class','style']});
  applyPalette();
  const resize=()=>{if(current()){const r=viewport.getBoundingClientRect();if(r.width>0&&r.height>0){const width=viewport.clientWidth||r.width;scene.resize(width,viewport.clientHeight||r.height,Math.min(settings.dpr,(window.devicePixelRatio||1)*r.width/width));if(selected)revealSelection();}}};
  const modal=element.closest('.modal');
  const positionSurface=()=>{
    const docked=modal?.matches('.modal-left-docked,.modal-right-docked')&&!modal.querySelector('.modal-content')?._chatVisibleMaximized;
    scene.setPlacement?.(docked?'docked':'expanded');
  };
  if(modal)listen(modal,'openclank:window-placement',positionSurface);
  positionSurface();
  const resizeObserver=new ResizeObserver(resize);resizeObserver.observe(viewport);
  const observer=new MutationObserver(()=>{if(!element.isConnected)dispose();});observer.observe(document.body,{childList:true,subtree:true});
  function invalidate(){resetProjectOverview();closeTaskContributions();projectionFailure=null;projectionStartedAt=projectionStartedKey=firstWitnessPaint=null;closeRemovalReview();clearDocumentEvidence();restoredFocusKey=null;clearRecordedActivity();clearEvidence();tablePage=0;geometryStamp='';stopProgression();projectionStart?.abort();automaticExpanded.clear();automaticContexts.clear();stopFollowing();closeObservedWorker();soloRoot=null;hiddenTerritories.clear();changeContext=null;activityScope='everything';activitySelect.value=activityScope;refreshSerial++;staging?.dispose();staging=null;refreshing=false;epoch++;sceneAuthorityKey=null;desiredSelected=null;retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};if(!options.settingsHost){settingsPanel.hidden=true;settingsDispose?.();settingsDispose=null;}expansionSerial++;anchor=null;expanded.clear();contexts.clear();hiddenChildren.clear();expansionBusy=false;expansionNotice='';actionSerial++;actionAborter.abort();actionAborter=new AbortController();capabilityAborter.abort();capabilityAborter=new AbortController();selected=null;query=null;sceneKey=null;renderedProjection=null;exportPanel.hidden=true;listPage=0;capability={available:false};capabilityKey=null;capabilityPending=null;model.reset();scene?.clear();legacy?.dispose();legacy=null;evidencePanel.replaceChildren();subscribers.clear();}
  function notifyEvidence(){for(const onStatus of subscribers)onStatus(copy(index));}
  function readLimits(){return {nodes:settings.pageNodes,edges:settings.pageEdges,groups:settings.pageGroups,bytes:settings.pageKiB*1024,work:settings.queryWork};}
  function configureObservation(){release?.configure?.({automatic:settings.autoRefresh,verifySeconds:settings.verifySeconds,cooldownSeconds:settings.cooldownSeconds,debounceMs:settings.debounceMs});release?.setVisible?.(hostVisible&&!document.hidden);}
  function applySettings(){
    const previous=settings;settings=resolveGraphSettings(settingsEnvelope,presentation);
    const expansionChanged=['retainedNodes','retainedEdges','cacheMiB','pageNodes','pageEdges','pageGroups','pageKiB','queryWork','initialDepth','autoNodes','autoReads','nodeScope','maxSymbols','maxEdges','direction'].some(key=>settings[key]!==previous[key])
      || Object.keys(settings).some(key=>key.startsWith('show')&&settings[key]!==previous[key]);
    const pageChanged=['pageNodes','pageEdges','pageGroups','pageKiB','queryWork'].some(key=>settings[key]!==previous[key]);
    if(expansionChanged)stopProgression();
    const relationKeys={containment:'showContainment',import:'showImport',call:'showCall',value_reference:'showValueReference',type_reference:'showTypeReference',inherits:'showInherits',implements:'showImplements',render:'showRender',route:'showRoute',overrides:'showOverrides',manifest_dependency:'showManifestDependency',sql_table_reference:'showSqlTableReference',callback_registration:'showCallbackRegistration',event_publish:'showEventPublish',event_consume:'showEventConsume'};
    relations.clear();for(const [relation,key]of Object.entries(relationKeys))if(settings[key])relations.add(relation);
    if(pageChanged)resetProjectOverview();
    if(model&&['retainedNodes','retainedEdges','cacheMiB','pageNodes','pageEdges','pageGroups','pageKiB','queryWork'].some(k=>settings[k]!==previous[k])){
      refreshSerial++;
      staging?.dispose();
      staging=null;
      refreshing=false;
      expansionSerial++;
      expansionBusy=false;
      expansionNotice='';
      model.configure({maxNodes:settings.retainedNodes,maxEdges:settings.retainedEdges,maxBytes:settings.cacheMiB*1048576,preservePaging:!pageChanged});
    }
    if(previous&&Object.keys(settings).some(k=>(k.startsWith('evidence')||k.startsWith('breadcrumb')||k.startsWith('search')||k==='queryWork')&&settings[k]!==previous[k]))legacy?.configureSettings?.();
    connectionDirection=settings.direction;configureObservation();
    const previousLayout=scene?.snapshot()?.presentation;
    scene?.setSettings?.(settings,presentation);
    if(previousLayout!==presentation||previous.grouping!==settings.grouping||previous.groupDepth!==settings.groupDepth)sceneKey=null;
    updateScene();paint();
    if(expansionChanged&&canLoadAutomatically()&&surfaceView!=='project'&&!soloRoot)startProgression();
  }
  function showSettings(focus=true){
    const panel=options.settingsHost || settingsPanel;
    panel.hidden=false;settingsDispose?.();const scope=workspace;
    settingsDispose=mountGraphSettings(panel,{mode:presentation,read:()=>settingsEnvelope,write:value=>{if(current()&&workspace===scope){settingsEnvelope=value;options.writeGraphSettings?.(value);}},isCurrent:()=>current()&&workspace===scope,onChange:applySettings});
    mountPromptBudget(panel,scope);
    if (!options.settingsHost) settingsPanel.prepend(button('Close settings',()=>{settingsPanel.hidden=true;settingsDispose?.();settingsDispose=null;}));
    if (focus) panel.querySelector('h3')?.focus();
  }
  async function focusResource(resource,{restore=false}={}){
    if(!restore)stopProgression();
    if(!current()||mapData.authority?.variant!=='published'||!resource?.id)return false;
    if(surfaceView==='project')switchSurface('canvas');else showView('map');
    const receipt=resource.receipt||mapData.authority.receipt;
    if(codeMapKey(receipt)!==codeMapKey(mapData.authority.receipt))return false;
    const ticket=++expansionSerial;
    const ok=await loadMap('neighbors',{root:{kind:'resource',resource_id:resource.id,receipt},expansionTicket:ticket});
    if(ok&&ticket===expansionSerial){const binding=mapData.selection;if(binding?.resource_id===resource.id){contexts.add(binding.visual_id);select(binding.visual_id,{requested:true,restore});return true;}}
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
  listen(document,'visibilitychange',()=>{syncVisibility();if(!document.hidden&&hostVisible)release?.dirty?.();});
  listen(window,'focus',()=>{if(hostVisible&&!document.hidden)release?.dirty?.();});
  listen(window,'openclank-files-watch-dirty',event=>{if(event.detail?.provider==='host'&&workspace)sourceHint(event.detail.resourceRef,'watch');});
  listen(window,'online',()=>{if(hostVisible)release?.dirty?.();});
  // Mutation hints never grant authority; ensure verifies bytes in the existing owner.
  listen(window,'openclank-files-resource-mutated',event=>{const resource=event.detail?.resource;if(!resource||(resource.provider||resource.key?.provider)!=='host'||!workspace)return;release?.dirty?.();
    if(event.detail?.action==='save')sourceHint(resource.ref||resource.locator?.opaqueRef||event.detail.resourceRef,'save');
    // Verification can consume this hint. Activity rows need their own receipt.
  });
  function activityContext(chatID=globalThis.window?.sessionModule?.getCurrentSessionId?.() ?? null){
    const published=mapData.authority?.receipt;
    const receipt=['ready','loading'].includes(mapData.status) && mapData.authority?.variant==='published' &&
      indexPresentation(index).usable && published?.workspace_id===workspace &&
      ['workspace_id','registration_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint']
        .every(key=>index[key]!=null && index[key]===published[key]) ? published : null;
    return {accountID:options.accountID,workspaceID:workspace,epoch,receipt,nodes:mapData.nodes,chatID};
  }
  function stopFollowing(){
    followActor=null;stopCamera.hidden=true;scene?.stopFollowing?.();scene?.setActivityTrail?.([]);
  }
  function closeObservedWorker(){
    actorRead?.abort();actorRead=null;actorInspection?.dispose();actorInspection=null;
    workerDetails.replaceChildren();workerDetails.hidden=true;
  }
  async function inspectObservedWorker(row,opener){
    closeObservedWorker();
    const scope=activityContext(),request=actorRead=new AbortController();
    const owned=()=>current()&&request===actorRead&&!request.signal.aborted&&scope.chatID===activityContext().chatID&&scope.epoch===epoch;
    workerDetails.hidden=false;workerDetails.textContent='Checking current worker identity…';
    try{
      const response=await fetch(`/api/orchestration/${encodeURIComponent(scope.chatID)}/observations/native_actor?limit=50`,{credentials:'same-origin',cache:'no-store',signal:request.signal});
      const value=response.ok?await response.json():null;
      if(!owned())return;
      const native=value?.scope,page=value?.sources?.native_actor;
      const actor=native?.hostChatId===scope.chatID&&native?.rootSessionId===row.engineSessionID&&Number.isSafeInteger(native.mapRevision)&&
        Array.isArray(page?.items) && page.items.find(item=>item?.sourceRef?.kind==='native_actor'&&
          item.sourceRef.sessionId===row.engineSessionID&&item.sourceRef.nativeId===row.actorID&&item.sourceRef.executionRevision===row.executionRevision);
      if(!actor)throw new Error();
      const [{openAgentRunInspector},{renderActorAccounting}]=await Promise.all([import('../agentRunInspector.js'),import('../chatRenderer.js')]);
      if(!owned())return;
      workerDetails.replaceChildren();
      actorInspection=openAgentRunInspector({container:workerDetails,scope:{chat:scope.chatID,mapRevision:native.mapRevision},
        row:actor,current:owned,opener,renderAccounting,
        onReturnToChat:()=>{closeObservedWorker();document.getElementById('message')?.focus();},
        onClose:closeObservedWorker});
    }catch{if(owned())workerDetails.textContent='The original worker revision is outside the current observation page.';}
  }
  function paintActivity(){
    if(!settings.agentActivity){activityModel.clear();stopFollowing();}
    activityModel.setContext(activityContext());
    const timeline=activityModel.timeline(), rows=timeline.items.filter(row=>row.kind==='read');
    const linked=rows.filter(row=>row.status==='linked'&&row.visualID);
    const currentActors=[...new Map(rows.map(row=>[actorKey(row),row])).values()].slice(-12);
    actors.replaceChildren(...currentActors.map(row=>button(row.actorID,()=>{
      if(row.visualID)select(row.visualID);
    },{'aria-label':`Select latest observed file for ${row.actorID}`,disabled:!row.visualID})));
    scene?.setObservedActors?.(currentActors.filter(row=>row.visualID).map(row=>row.visualID));
    const focused=activityList.contains(document.activeElement)?document.activeElement.dataset.activityControl:null;
    if(followActor&&!linked.some(row=>actorKey(row)===followActor))stopFollowing();
    scene?.setActivityCoverage?.(linked.map(row=>row.visualID),activityScope==='chat');
    scene?.setActivityTrail?.(settings.activityTrail&&followActor
      ?linked.filter(row=>actorKey(row)===followActor).map(row=>row.visualID):[]);
    enableReads.setAttribute('aria-pressed',String(settings.agentActivity));
    if(activityScope==='changed'){
      const changed=recordedChangeNode();
      activityList.replaceChildren(el('p',{text:changed
        ?`Recorded comparison · ${changeContext.actionID} · ${changeContext.versionID}`
        :'Choose a recorded change from History before filtering by it.'}));
      return;
    }
    if(!settings.agentActivity){activityList.replaceChildren();return;}
    const contextLabels={compact:'Context compacted',clear:'Context cleared'};
    activityList.replaceChildren(el('p',{text:'Observed workspace activity from this chat · partial coverage'}),
      ...timeline.items.map(row=>{
        const time=new Date(row.observedAt).toLocaleTimeString();
        if(row.kind==='context_boundary')return el('p',{text:`${time} · ${contextLabels[row.operation]}`});
        const label=`${time} · Read observed · ${row.status} · actor ${row.actorID} · turn ${row.turnID}${
          row.rereadAfter?` · reread after context ${row.rereadAfter.operation}`:''}`;
        const line=el('div',{class:'code-map-toolbar'},el('span',{text:label}));
        const inspect=button('Worker details',()=>void inspectObservedWorker(row,inspect),{'data-activity-control':'view:'+row.key});line.append(inspect);
        if(row.visualID)line.append(button('Follow',()=>{
          if(scene?.followNode?.(row.visualID)){followActor=actorKey(row);stopCamera.hidden=false;paintActivity();}
        },{'aria-label':`Follow ${row.actorID} in its observed session`,'data-activity-control':'follow:'+row.key}));
        return line;
      }));
    if(focused)[...activityList.querySelectorAll('[data-activity-control]')].find(node=>node.dataset.activityControl===focused)?.focus({preventScroll:true});
  }
  function observeActivity(event,chatID){
    if(!current()||!settings.agentActivity||chatID!==globalThis.window?.sessionModule?.getCurrentSessionId?.())return false;
    const row=activityModel.add(event,activityContext(chatID),settings.activityCount);
    if(!row)return false;
    paintActivity();
    if(row.status==='linked'&&row.visualID){
      scene?.activity?.([row.visualID],{agentRead:true});
      if(followActor===actorKey(row))scene?.followNode?.(row.visualID);
    }
    return true;
  }
  listen(document,'odysseus:session-selected',()=>{if(current()){stopFollowing();closeObservedWorker();scene?.clearAgentActivity?.();paintActivity();}});
  function startIndex({preserve=false}={}){
    const ticket=++indexSerial,id=workspace;release?.();release=null;loadedKey=null;if(!preserve)invalidate();index={lifecycle:id?'starting':'missing'};paint();
    if(!id||!current()||!subscribeIndex)return;
    const owned=subscribeIndex(id,{automatic:settings.autoRefresh,isCurrent:()=>current()&&ticket===indexSerial&&id===workspace,onStatus:value=>{
      if(!current()||ticket!==indexSerial||id!==workspace)return;
      const before=codeMapIndexKey(index),previousHead=expectedPublishedHead();index=value||{lifecycle:'unavailable'};
      const revoked=['erased','denied','missing'].includes(index.lifecycle),key=codeMapIndexKey(index);
      const publishedHead=expectedPublishedHead();
      const publication=publishedHead&&publishedHead!==previousHead&&mapData.authority?.receipt?.head_generation!==publishedHead;
      if(revoked){invalidate();loadedKey=null;}
      else if(before!==key||publication){
        projectionFailure=null;
        stopProgression();
        projectionStart?.abort();
        loadedKey=null;
        mapFailures=0;nextMapAttempt=0;
        actionSerial++;actionAborter.abort();actionAborter=new AbortController();
      }
      notifyEvidence();paint();
      if(attemptKey!==key){attemptKey=key;mapFailures=0;nextMapAttempt=0;}
      if(!revoked) startVisibleProjection();
    }});
    if(!current()||ticket!==indexSerial)owned?.();else release=owned;configureObservation();paint();
  }
  function expectedPublishedHead(){
    return index.lifecycle==='ready'&&['current','partial'].includes(index.freshness)?index.head_generation:null;
  }
  function projectionStatus(){
    if(retryingOverview && projectionStart===retryingOverview && !retryingOverview.signal.aborted)return 'The folder overview timed out. Retrying the current index read…';
    if(refreshing)return mapData.authority?.variant==='published'?'Reading the new generation. The previous map remains visible.':'Reading the published graph. The folder preview remains visible.';
    const failure=projectionFailure?.key===codeMapIndexKey(index)?projectionFailure.error:null;
    if(failure){
      if(failure.code==='reindex_required' && mapData.authority?.variant==='structure_preview')
        return codeMapStatus(index,mapData);
      const retained=mapData.authority?.variant==='published'?'Previous graph remains visible.':mapData.authority?.variant==='structure_preview'?'Folder preview remains visible.':'';
      return [codeMapStatus(index,{status:'error',error:failure}),retained].filter(Boolean).join(' ');
    }
    if(mapData.authority?.variant==='published'&&index.head_generation&&mapData.authority.receipt.head_generation!==index.head_generation)return 'Previous published view. Retry map read to load the current generation.';
    return codeMapStatus(index,mapData);
  }

  function canLoadAutomatically(){
    return current()&&hostVisible&&!document.hidden&&view==='map'&&!layoutPaused&&codeMapMayRead(index);
  }

  function startVisibleProjection(){
    if(!canLoadAutomatically()||projectionStart)return;
    const key=codeMapIndexKey(index);
    if(loadedKey===key){runProgression();return;}
    if(mapFailures>=3||Date.now()<nextMapAttempt)return;
    const ticket=indexSerial,id=workspace,controller=projectionStart=new AbortController();
    if(projectionStartedKey!==key){
      projectionStartedKey=key;projectionStartedAt=performance.now();firstWitnessPaint=null;
    }
    const owns=()=>current()&&ticket===indexSerial&&id===workspace&&loadedKey===key;
    loadedKey=key;
    void (async()=>{
      try {
        const completed=mapData.authority
          ? await refreshPublishedGraph(key,{signal:controller.signal})
          : await initialExpansion({signal:controller.signal,deferProgression:true});
        if(!owns())return;
        const expected=expectedPublishedHead();
        const mismatched=completed&&expected&&mapData.authority?.receipt?.head_generation!==expected;
        if(mismatched)projectionFailure={key,error:{code:mapData.authority?.variant==='structure_preview'?'reindex_required':'stale_cursor'}};
        if(!completed||mismatched){
          loadedKey=null;
          if(!controller.signal.aborted){
            mapFailures = index.lifecycle === 'ready' ? 3 : mapFailures + 1;
            nextMapAttempt=Date.now()+Math.min(30000,1000*2**mapFailures);
          }
          return;
        }
        if(controller.signal.aborted)return;
        projectionFailure=null;
        if(focusPath)await focusRequestedPath();
        else if(!selected){
          const receipt=qualifiedReceipt(), receiptKey=codeReceiptKey(receipt);
          if(receipt&&receiptKey!==restoredFocusKey){
            restoredFocusKey=receiptKey;
            const id=geometry.focus(receipt);
            if(id)await focusResource({id,receipt},{restore:true});
          }
        }
        if(owns()&&!controller.signal.aborted) startProgression();
      } finally {
        if(projectionStart===controller){
          projectionStart=null;
          if(canLoadAutomatically()&&(!owns()||controller.signal.aborted)) startVisibleProjection();
        }
      }
    })();
  }

  function nodeIntent(id,data=mapData,byId=new Map(data.nodes.map(n=>[n.id,n]))){
    const node=byId.get(id);if(!node)return null;
    const path=[],seen=new Set();let n=node;
    while(n&&n.kind!=='repository'&&!seen.has(n.id)&&seen.size<64){seen.add(n.id);if(n.kind!=='symbol'){if(new TextEncoder().encode(n.label).length>=157)return null;path.unshift(n.label);}n=byId.get(n.parent);}
    if(!n||n.kind!=='repository')return null;
    return {kind:node.kind,path,resource:node.resource_id||null};
  }
  const intentKey=value=>value?codeMapKey(value.kind==='symbol'?{kind:value.kind,resource:value.resource}:{kind:value.kind,path:value.path}):'';
  function rebindRetainedIntent(){
    const byId=new Map(mapData.nodes.map(n=>[n.id,n]));
    const lookup=new Map(mapData.nodes.map(n=>[intentKey(nodeIntent(n.id,mapData,byId)),n]));
    for(const [targets,set] of [[retainedIntent.expanded,expanded],[retainedIntent.hidden,hiddenChildren],[retainedIntent.contexts,contexts]])for(const target of targets){const n=lookup.get(intentKey(target));if(n)set.add(n.id);}
    if(!selected&&retainedIntent.selection){const n=lookup.get(intentKey(retainedIntent.selection));if(n)selected=n.id;}
  }
  async function refreshPublishedGraph(key,{signal}={}){
    const ticket=++refreshSerial,scope=identity(),intentRevision=expansionSerial,owned=()=>!signal?.aborted&&current()&&intentRevision===expansionSerial&&ticket===refreshSerial&&codeMapKey(scope)===codeMapKey(identity())&&loadedKey===key;
    const old=model,intent={expanded:[...retainedIntent.expanded,...[...expanded].filter(id=>id!==anchor&&!automaticExpanded.has(id)).map(id=>nodeIntent(id)).filter(Boolean)],hidden:[...retainedIntent.hidden,...[...hiddenChildren].map(id=>nodeIntent(id)).filter(Boolean)],contexts:[...retainedIntent.contexts,...[...contexts].filter(id=>!automaticContexts.has(id)).map(id=>nodeIntent(id)).filter(Boolean)],selection:nodeIntent(selected)||retainedIntent.selection};
    if([...hiddenChildren].some(id=>!nodeIntent(id))){expansionNotice='Cannot safely restore a hidden branch with an incomplete or truncated path. Reset Overview before refreshing.';paint();return false;}
    for(const key of ['expanded','hidden','contexts'])intent[key]=[...new Map(intent[key].filter(Boolean).map(n=>[intentKey(n),n])).values()];
    if(intent.expanded.length+intent.hidden.length+intent.contexts.length>4000||new TextEncoder().encode(JSON.stringify(intent)).length>524288){expansionNotice='Previous branch intent exceeds the refresh budget. Reset Overview or reduce expanded branches before refreshing.';paint();return false;}
    let data={nodes:[],edges:[],authority:null},reads=0;refreshing=true;paint();staging?.dispose();
    const staged=createCodeMapModel({...modelOptions,isCurrent:()=>model===staged?current()&&codeMapKey(scope)===codeMapKey(identity()):owned(),getPinned:()=>model===staged?[anchor,selected].filter(Boolean):[],maxNodes:settings.retainedNodes,maxEdges:settings.retainedEdges,maxBytes:settings.cacheMiB*1048576,changed:value=>{data=value;if(model===staged&&!suppressModel){mapData=value;rebindRetainedIntent();if(selected&&!value.nodes.some(n=>n.id===selected))selected=null;if(!deferProjectionPaint){updateScene();paint();}}}});staging=staged;
    const read=async(operation,root=null,more=false)=>{if(!owned()||reads>=settings.autoReads)return false;reads++;const ok=await staged.run(workspace,{schema:MAP_SCHEMA,operation,root,expected_authority:operation==='overview'&&!more?null:data.authority,limits:readLimits()},{more,signal,missingIsAbsent:root?.kind==='resource'});if(['denied','erased'].includes(data.error?.code)){const code=data.error.code;invalidate();index={lifecycle:code};paint();}return ok;};
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
      const overviewReady=await read('overview');
      if(!owned())return false;
      if(!overviewReady){
        projectionFailure={key,error:data.error||{code:'unavailable'}};
        return false;
      }
      const expected=expectedPublishedHead();
      if(expected&&data.authority?.receipt?.head_generation!==expected){
        projectionFailure={key,error:{code:data.authority?.variant==='structure_preview'?'reindex_required':'stale_cursor'}};
        return false;
      }
      projectionFailure=null;
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
      const previousNodes=new Map(mapData.nodes.map(n=>[n.id,n])),nextNodes=new Map(data.nodes.map(n=>[n.id,n]));
      const crossesPreview=mapData.authority.variant==='structure_preview'||data.authority.variant==='structure_preview';
      const geometryKey=(node,projection,items)=>node.resource_id&&(!crossesPreview||node.kind==='symbol')
        ?JSON.stringify([node.kind,node.resource_id]):intentKey(nodeIntent(node.id,projection,items));
      const newGeometry=new Map();
      for(const node of data.nodes){
        const key=geometryKey(node,data,nextNodes);
        if(key)newGeometry.set(key,newGeometry.has(key)?null:node.id);
      }
      const remap=id=>{const node=previousNodes.get(id);return node&&newGeometry.get(geometryKey(node,mapData,previousNodes));};
      if(settings.preserveCamera)scene?.remapPositions?.(remap);
      const nextSolo=soloRoot?remap(soloRoot):null;
      const nextHiddenTerritories=[...hiddenTerritories].map(remap).filter(Boolean);
      suppressModel=true;old.dispose();suppressModel=false;model=staged;retainedIntent=intent;staging=null;automaticExpanded.clear();automaticContexts.clear();expanded.clear();for(const id of reboundExpanded)expanded.add(id);hiddenChildren.clear();for(const id of reboundHidden)hiddenChildren.add(id);contexts.clear();for(const id of reboundContexts)contexts.add(id);
      anchor=data.nodes.find(n=>n.kind==='repository')?.id||data.root;if(anchor)expanded.add(anchor);selected=chosen?.id||null;desiredSelected=null;mapData=data;rebindRetainedIntent();query=null;sceneKey=null;
      soloRoot=nextSolo;hiddenTerritories.clear();for(const id of nextHiddenTerritories)hiddenTerritories.add(id);expansionNotice=reads>=settings.autoReads?'Previous context restored within the automatic read budget; some branches/connections may require explicit expansion or continuation.':'Previous context rebound; missing or changed declarations are not substituted.';updateScene();paint();return true;
    }finally{if(staging===staged){staged.dispose();staging=null;}if(ticket===refreshSerial){refreshing=false;paint();}}
  }
  async function retryMapRead(){
    stopProgression();
    mapFailures=0;nextMapAttempt=0;loadedKey=codeMapIndexKey(index);
    const ok=mapData.authority?await refreshPublishedGraph(loadedKey):await initialExpansion({deferProgression:true});
    if(ok)startProgression();
    else{loadedKey=null;mapFailures++;nextMapAttempt=Date.now()+2000;}
    return ok;
  }
  function stopRequestedFocus({keepProgression=false}={}){stopProgression({keep:keepProgression});focusSerial++;focusPath='';focusMessage='';}
  function selectWorkspace(id,{label:nextLabel=id}={}){if(!current())return;stopRequestedFocus();if(layoutPaused){layoutPaused=false;scene.setLayoutPaused?.(false);}folderSerial++;workspace=id;geometry=workspaceGeometry();label=nextLabel;workspaceName.textContent=label;setWorkspace(id,label);settingsEnvelope=normalizeGraphSettings(options.readGraphSettings?.());presentation=settingsEnvelope.presentation;presentationSelect.value=presentation;applySettings();showView('map');startIndex();if(options.settingsHost)showSettings(false);}
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
  function setSolo(id){
    stopProgression();
    if(id&&!mapData.nodes.some(n=>n.id===id&&['folder','repository'].includes(n.kind)))return;
    soloRoot=id;updateScene();paint();
  }
  function hideTerritory(id){
    stopProgression();
    const node=mapData.nodes.find(n=>n.id===id&&n.kind==='folder');
    if(!node)return;
    if(soloRoot===id)soloRoot=null;
    hiddenTerritories.add(id);
    updateScene();
    if(node.parent)select(node.parent,{requested:true});
    paint();
  }
  function restoreTerritories(){hiddenTerritories.clear();setSolo(null);}
  function exploration(){return {anchor,expanded,contexts,hiddenChildren,hiddenTerritories,relations,solo:soloRoot};}
  function currentChange(){
    return !!changeContext&&indexPresentation(index).usable&&codeReceiptKey(mapData.authority?.receipt)===changeContext.receipt
      &&mapData.authority?.receipt?.head_generation===index.head_generation;
  }
  function recordedChangeNode(){
    return currentChange()?mapData.nodes.find(node=>node.kind==='file'&&node.resource_id===changeContext.resourceID)||null:null;
  }
  function visibleMap(){
    const data=codeMapVisible(mapData,exploration()),byId=new Map(data.nodes.map(n=>[n.id,n]));
    let fileCount=0,symbolCount=0;
    const pins=new Set([selected,anchor]);
    let parent=byId.get(selected)?.parent;
    while(parent&&byId.has(parent)&&!pins.has(parent)){
      pins.add(parent);parent=byId.get(parent).parent;
    }
    const ordered=data.nodes.slice().sort((a,b)=>Number(pins.has(b.id))-Number(pins.has(a.id)));
    let allowed=ordered.filter(n=>{if(n.kind==='file')return ++fileCount<=settings.maxFiles;if(n.kind!=='symbol')return true;
      if(settings.nodeScope==='files')return false;const kind=n.symbol_kind||'';
      if(/function|method/.test(kind)&&!settings.showFunctions||/class|interface|type/.test(kind)&&!settings.showClasses||!/function|method|class|interface|type/.test(kind)&&!settings.showOtherSymbols)return false;
      return ++symbolCount<=settings.maxSymbols;});
    if(settings.nodeScope==='symbols'){const keep=new Set(data.nodes.filter(n=>n.kind==='symbol').map(n=>n.id));for(const id of [...keep]){let p=byId.get(id)?.parent;const seen=new Set();while(p&&byId.has(p)&&!seen.has(p)){seen.add(p);keep.add(p);p=byId.get(p).parent;}}allowed=allowed.filter(n=>keep.has(n.id)||n.kind==='repository');}
    if(retainedIntent.hidden.length){
      const allNodes=new Map(mapData.nodes.map(n=>[n.id,n]));
      allowed=allowed.filter(n=>{const intent=nodeIntent(n.id,mapData,allNodes);return intent&&!retainedIntent.hidden.some(h=>h&&h.kind!=='symbol'&&h.path.length<intent.path.length&&h.path.every((part,i)=>part===intent.path[i]));});
    }
    if(activityScope==='changed'){
      const changed=recordedChangeNode();
      allowed=changed?allowed.filter(n=>n.id===changed.id||descendantOf(n.id,changed.id,byId)||descendantOf(changed.id,n.id,byId)):[];
    }
    allowed=allowed.slice(0,settings.maxNodes);
    const ids=new Set(allowed.map(n=>n.id));let relevant=data.edges.filter(e=>ids.has(e.from)&&ids.has(e.to));
    if(selected&&settings.direction!=='both')relevant=relevant.filter(e=>e.relation==='containment'||(settings.direction==='incoming'?e.to===selected:e.from===selected));
    const edgeRank=edge=>edge.relation==='containment'?1:edge.from===selected||edge.to===selected?0:edge.state==='exact'?2:3;
    relevant.sort((a,b)=>edgeRank(a)-edgeRank(b));
    let connections=0;relevant=relevant.filter(e=>e.relation==='containment'||!selected||e.from!==selected&&e.to!==selected||++connections<=settings.connections).slice(0,settings.maxEdges);
    return {...data,nodes:allowed,edges:relevant,displayOmitted:data.nodes.length-allowed.length,edgeOmitted:data.edges.length-relevant.length};
  }
  function updateScene(){
    if(!scene)return;
    if(!mapData.authority){loadedNodes=loadedAuthority=null;seenLoadedNodes.clear();scene.clear();sceneKey=sceneAuthorityKey=null;renderedProjection=null;return;}
    if(!['ready','loading','error'].includes(mapData.status))return;
    const authority=codeMapKey(mapData.authority);
    const discovered=new Set();
    if(loadedAuthority!==authority){
      seenLoadedNodes.clear();loadedNodes=null;loadedAuthority=authority;
    }
    if(loadedNodes!==mapData.nodes){
      loadedNodes=mapData.nodes;
      // Observe the retained projection before applying visibility filters.
      // Keep discovery bookkeeping bounded even if cache pages are evicted.
      for(const {id} of loadedNodes){
        if(seenLoadedNodes.size>=CODE_GRAPH_DISPLAY.nodes*4)break;
        if(!seenLoadedNodes.has(id)){seenLoadedNodes.add(id);discovered.add(id);}
      }
    }
    const visible=visibleMap(),key=codeMapKey({authority:visible.authority,nodes:visible.nodes.map(n=>n.id),edges:visible.edges.map(e=>e.id)});
    if(key!==sceneKey){
      const sourceItems=new Set(visible.nodes.filter(node=>['file','symbol'].includes(node.kind)).map(node=>node.id));
      const dto={authority:visible.authority,status:'ready',
        nodes:visible.nodes.map(({id,label,kind,parent,symbol_kind})=>({id,label,kind,parent,...(symbol_kind?{symbol_kind}:{})})),
        edges:visible.edges.map(({from,to,relation,state,selection_id})=>({from,to,relation,state,
          ...(visible.authority.variant==='published'&&state==='exact'&&relation!=='containment'&&
            typeof selection_id==='string'&&sourceItems.has(from)&&sourceItems.has(to)?{witnessed:true}:{})})),
        retainPresentation:settings.preserveCamera&&sceneAuthorityKey!==null};
      const fits=()=>new TextEncoder().encode(JSON.stringify(dto)).length<=CODE_GRAPH_DISPLAY.sceneBytes;
      while(!fits()&&dto.edges.length)dto.edges.length=Math.floor(dto.edges.length*.75);
      while(!fits()&&dto.nodes.length>1){dto.nodes.length=Math.max(1,Math.floor(dto.nodes.length*.75));const ids=new Set(dto.nodes.map(n=>n.id));dto.edges=dto.edges.filter(e=>ids.has(e.from)&&ids.has(e.to));}
      sceneByteNotice=dto.nodes.length<visible.nodes.length||dto.edges.length<visible.edges.length?`Scene byte limit: ${visible.nodes.length-dto.nodes.length} nodes / ${visible.edges.length-dto.edges.length} edges omitted; retained evidence unchanged.`:'';
      const layout=mapGeometry(dto.nodes);
      try{scene.update(dto,{...layout,discovered:dto.nodes.filter(n=>discovered.has(n.id)).map(n=>n.id)});sceneKey=key;renderedProjection=copy(dto);}catch{sceneKey=null;renderedProjection=null;sceneByteNotice='Scene budget unavailable; reduce displayed nodes or edges.';}
    }
    sceneAuthorityKey=authority;
    syncTracedEdges();
    scene.select(selected);revealSelection();
  }
  function mountPromptBudget(panel,scope){
    if(typeof options.readPromptBudget!=='function'||typeof options.writePromptBudget!=='function')return;
    const section=el('details',{class:'graph-settings-group'},el('summary',{text:'Graph prompt context'}));
    const id=`graph-prompt-${crypto.randomUUID()}`;
    const slider=el('input',{id,type:'range',min:768,max:24000,step:1,value:24000,'aria-label':'Graph prompt budget in tokens'});
    const exact=el('input',{type:'number',min:768,max:24000,step:1,value:24000,'aria-label':'Graph prompt token budget exact value'});
    const message=el('p',{role:'status',text:'Reading requested budget…'});
    const currentBudget=()=>current()&&workspace===scope&&section.isConnected;
    let saved=24000;
    const valid=value=>Number.isSafeInteger(value)&&value>=768&&value<=24000;
    const set=value=>{slider.value=exact.value=value;};
    function describe(){
      message.textContent=`Requested ${saved.toLocaleString()} tokens. Effective use also depends on the active model's available context and admitted evidence.`;
    }
    const commit=async input=>{
      if(!currentBudget())return;
      const value=input.valueAsNumber;
      if(!valid(value)){set(saved);message.textContent='Choose 768 to 24,000 tokens.';return;}
      if(value===saved)return;
      slider.disabled=exact.disabled=true;
      try{
        const result=await options.writePromptBudget(value);
        if(!currentBudget())return;
        if(!valid(result))throw new Error();
        saved=result;set(saved);describe();
      }catch{if(currentBudget()){set(saved);message.textContent='Graph prompt budget could not be saved.';}}
      finally{if(currentBudget())slider.disabled=exact.disabled=false;}
    };
    slider.disabled=exact.disabled=true;
    slider.addEventListener('input',()=>{exact.value=slider.value;});
    slider.addEventListener('change',()=>void commit(slider));
    exact.addEventListener('change',()=>void commit(exact));
    section.append(el('label',{for:id,text:'Tokens offered to the prompt'}),
      el('div',{class:'graph-setting-inputs'},slider,exact),
      el('div',{class:'graph-setting-bounds'},el('span',{text:'768'}),el('span',{text:'24,000'})),message);
    (panel.querySelector('.graph-settings-groups')||panel).append(section);
    Promise.resolve().then(()=>options.readPromptBudget()).then(value=>{
      if(!currentBudget())return;
      if(!valid(value))throw new Error();
      saved=value;set(saved);describe();slider.disabled=exact.disabled=false;
    }).catch(()=>{if(currentBudget())message.textContent='Current graph prompt budget unavailable.';});
  }

  function clearRecordedActivity(){
    recordedRead?.abort();recordedRead=null;recordedReceipt=null;
    recordedPanel.hidden=true;recordedPanel.replaceChildren();
  }

  async function showRecordedActivity(){
    const receipt=qualifiedReceipt(), workspaceId=workspace;
    if(!receipt||typeof options.readRecordedActivity!=='function')return;
    clearRecordedActivity();
    const controller=recordedRead=new AbortController(), key=recordedReceipt=codeReceiptKey(receipt);
    const owns=()=>current()&&recordedRead===controller&&!controller.signal.aborted&&codeReceiptKey(qualifiedReceipt())===key;
    const pages=[], consumed=new Set(), visibleActions=new Set();
    const readStatus=el('p',{role:'status'});
    const itemList=el('ol',{'aria-label':'Qualified recorded edits'});
    const correlations=el('div',{'aria-label':'Recurring co-change by activity page'});
    const saveStatus=el('p',{role:'status'});
    const title=el('input',{type:'text',maxlength:120,'aria-label':'Investigation title',placeholder:'Investigation title'});
    let cursor=null, reading=false, saving=false, saved=false;
    const moreButton=button('Load more recorded activity',()=>void loadPage(cursor),{hidden:true});
    const saveButton=button('Save investigation',()=>void saveInvestigation(),{disabled:true});
    const saveControls=el('div',{class:'code-map-toolbar'},title,saveButton);
    const close=button('Close recorded activity',clearRecordedActivity);
    listen(title,'input',()=>{saved=false;paintControls();});
    recordedPanel.hidden=false;
    recordedPanel.replaceChildren(el('p',{text:'Committed edits · partial Lore activity'}),readStatus,itemList,correlations,moreButton,
      ...(typeof options.saveRecordedInvestigation==='function'?[saveControls,saveStatus]:[]),close);
    recordedPanel.focus({preventScroll:true});

    function paintControls(){
      moreButton.hidden=!cursor||pages.length>=4;
      moreButton.disabled=reading||saving;
      saveButton.disabled=!pages.length||!visibleActions.size||!title.value.trim()||reading||saving||saved;
    }

    async function saveInvestigation(){
      if(!owns()||saveButton.disabled)return;
      const name=title.value.trim();
      saving=true;paintControls();
      saveStatus.textContent='Saving a Lore-backed investigation…';
      try{
        const result=await options.saveRecordedInvestigation({workspaceId,receipt,pages:pages.slice(),title:name,isCurrent:owns});
        if(!owns())return;
        if(typeof result?.documentId!=='string'||!result.documentId)throw new Error();
        saved=true;
        saveStatus.replaceChildren(el('span',{text:'Saved investigation. '}),
          ...(typeof result.open==='function'?[button('Open investigation',()=>{if(owns())void result.open();})]:[]));
      }catch{if(owns())saveStatus.textContent='The investigation could not be saved from these activity pages.';}
      finally{if(owns()){saving=false;paintControls();}}
    }

    async function loadPage(continuation){
      if(!owns()||reading||saving||pages.length>=4||consumed.has(continuation))return;
      reading=true;paintControls();
      readStatus.textContent='Reading recorded activity…';
      try{
        const value=await options.readRecordedActivity({workspaceId,receipt,signal:controller.signal,isCurrent:owns,...(continuation?{cursor:continuation}:{})});
        if(!owns())return;
        const scope=value?.projection?.graph_scope;
        if(value?.version!==1||value.coverage!=='partial_recorded_activity'||scope?.workspace_id!==workspaceId
            ||scope.checkout_id!==receipt.checkout_id||scope.policy_generation!==receipt.policy_generation
            ||Number.isSafeInteger(index.workspace_revision)&&scope.workspace_revision!==index.workspace_revision
            ||!Array.isArray(value.items)||value.items.length>20)throw new Error();
        consumed.add(continuation);
        pages.push(value);
        cursor=typeof value.next_cursor==='string'&&value.next_cursor.startsWith('wa1.')&&value.next_cursor.length<=16384&&!consumed.has(value.next_cursor)
          ?value.next_cursor:null;
        saved=false;
        const validID=id=>typeof id==='string'&&/^file:[0-9a-f]{64}$/.test(id);
        const joined=new Map(joinCodeActivityHistory(value,activityModel.snapshot(),activityContext()).map(row=>[
          JSON.stringify([row.action_id,row.code_resource_id]),row
        ]));
        for(const row of value.items){
          if(row?.state!=='complete'||!validID(row.code_resource_id)||typeof row.action_id!=='string'
              ||typeof row.name!=='string'||!/^[0-9a-f]{64}$/.test(row.witness_sha256||''))continue;
          const mark=JSON.stringify([row.action_id,row.code_resource_id]);
          if(visibleActions.has(mark))continue;
          visibleActions.add(mark);
          const visual=mapData.nodes.find(node=>node.resource_id===row.code_resource_id), at=new Date(Number(row.recorded_millis));
          const who=row.origin?.actor_id, read=joined.get(mark);
          const readCoverage=read?.coverage==='same_revision_read_observed'?' · same committed revision read observed':read?' · read coverage unknown':'';
          const openChange=typeof row.locator==='string'?capturedChangeButton(row.locator,{isCurrent:owns}):null;
          itemList.append(el('li',{},
            el('span',{text:`${row.name.slice(0,160)}${typeof who==='string'?' · '+who.slice(0,100):''}${row.recorded_millis!=null&&Number.isFinite(at.valueOf())?' · '+at.toLocaleString():''}${readCoverage} `}),
            ...(visual?[button('Select file',()=>{if(owns())select(visual.id);})]:[]),
            ...(openChange?[openChange]:[])));
        }
        const {cochanges,eligible_changesets:eligible}=value.projection;
        const cochangesList=el('ul');
        const names=new Map(mapData.nodes.map(node=>[node.resource_id,node.label]));
        if(Array.isArray(cochanges)&&Number.isSafeInteger(eligible)){
          for(const row of cochanges.slice(0,20)){
            if(row?.kind!=='recorded_cochange'||!validID(row.left)||!validID(row.right)
                ||!Number.isInteger(row.support)||row.support<2||row.denominator!==eligible||row.support>eligible)continue;
            cochangesList.append(el('li',{text:`${names.get(row.left)||'Other recorded file'} ↔ ${names.get(row.right)||'Other recorded file'} · ${row.support} of ${eligible} eligible changesets`}));
          }
        }
        if(cochangesList.childElementCount)correlations.append(el('strong',{text:`Recurring co-change in page ${pages.length}`}),cochangesList);
        readStatus.textContent=visibleActions.size?
          `${visibleActions.size} qualified edits across ${pages.length} loaded activity page${pages.length===1?'':'s'}${cursor&&pages.length>=4?' · review limit reached':''}.`:
          'No qualified edits were present in the loaded activity.';
      }catch{if(owns())readStatus.textContent=continuation?'The next activity page is unavailable. Try again or close this review.':'Recorded activity is unavailable for this workspace.';}
      finally{if(owns()){reading=false;paintControls();}}
    }
    await loadPage(null);
  }

  function publishedContext(){
    const receipt=qualifiedReceipt();
    return current()&&receipt ? {workspaceId:workspace,receipt,resourceID:model.bindingFor(selected)?.resource_id||null} : null;
  }

  function clearDocumentEvidence(){
    citationRead?.abort();citationRead=null;citationKey=null;
    citationsPanel.hidden=true;citationsPanel.replaceChildren();
  }

  async function showDocumentEvidence(){
    const context=publishedContext();
    if(!context?.resourceID||typeof options.readDocumentEvidence!=='function')return;
    clearDocumentEvidence();
    const {workspaceId,receipt,resourceID:resourceId}=context;
    const controller=citationRead=new AbortController(), key=citationKey=JSON.stringify([codeReceiptKey(receipt),resourceId]);
    const owns=()=>current()&&citationRead===controller&&!controller.signal.aborted&&
      key===JSON.stringify([codeReceiptKey(qualifiedReceipt()),model.bindingFor(selected)?.resource_id]);
    citationsPanel.hidden=false;
    citationsPanel.replaceChildren(el('p',{text:'Reading scoped document citations…'}));
    citationsPanel.focus({preventScroll:true});
    const close=button('Close citations',clearDocumentEvidence);
    try{
      const value=await options.readDocumentEvidence({workspaceId,receipt,resourceId,signal:controller.signal,isCurrent:owns});
      if(!owns())return;
      if(!Array.isArray(value?.citations))throw new Error();
      const available=value.citations.slice(0,64).filter(row=>row&&typeof row.documentId==='string'&&typeof row.revisionId==='string'&&
        typeof row.name==='string'&&typeof row.spelling==='string'&&Number.isSafeInteger(row.startByte)&&Number.isSafeInteger(row.endByte)&&row.startByte>=0&&row.endByte>row.startByte);
      const rows=available.map(row=>el('li',{},typeof options.openDocumentCitation==='function'?
        button(row.name.slice(0,256)+' · '+row.spelling.slice(0,128),()=>{if(owns())void options.openDocumentCitation(row,{workspaceId,receipt,resourceId,isCurrent:owns});}):
        el('span',{text:row.name.slice(0,256)+' · '+row.spelling.slice(0,128)})));
      const isPartial=value.coverage?.complete!==true||available.length!==value.citations.length;
      const omitted=[['documents',value.coverage?.omittedDocuments],['mentions',value.coverage?.omittedMentions]]
        .filter(([,count])=>Number.isSafeInteger(count)&&count>0).map(([unit,count])=>count+' '+unit+' omitted').join(' · ');
      citationsPanel.replaceChildren(el('p',{text:isPartial?['Partial document citation coverage',omitted].filter(Boolean).join(' · '):'Current document citations'}),
        rows.length?el('ul',{},...rows):el('p',{text:'No resolved citations in this read.'}),close);
    }catch{if(owns())citationsPanel.replaceChildren(el('p',{text:'Document citations are unavailable for this selection.'}),close);}
  }

  function closeRemovalReview(){
    removalReviewSerial++;
    removalReview.hidden=true;
    removalReview.replaceChildren();
  }

  function removalReviewRequired(){
    const code=index.error?.code;
    return ['removal_confirmation_required','stale_removal_confirmation'].includes(code) &&
      typeof release?.previewRemovals==='function' && typeof release?.confirmRemovals==='function';
  }

  async function openRemovalReview(){
    if(!current()||!removalReviewRequired())return;
    closeRemovalReview();
    const serial=removalReviewSerial, subscription=release, scope=codeMapIndexKey(index);
    const owns=()=>current()&&serial===removalReviewSerial&&subscription===release&&scope===codeMapIndexKey(index);
    const cancel=button('Cancel',closeRemovalReview);
    removalReview.hidden=false;
    removalReview.replaceChildren(el('p',{text:'Reading the current removal preview…'}),cancel);
    removalReview.focus({preventScroll:true});
    try{
      const preview=await subscription.previewRemovals();
      if(!owns())return;
      if(preview?.required!==true||typeof preview.confirmation!=='string'||!preview.confirmation.length||
          typeof preview.prior_generation!=='string'||!Number.isSafeInteger(preview.prior_files)||
          !Number.isSafeInteger(preview.removed_files)||preview.removed_files<1||preview.removed_files>preview.prior_files){
        removalReview.replaceChildren(el('p',{text:'There is no current removal preview to confirm.'}),cancel);
        return;
      }
      const paths=Array.isArray(preview.sample_paths)?preview.sample_paths.filter(path=>typeof path==='string').slice(0,16):[];
      const note=el('p',{role:'status',text:`${preview.removed_files} of ${preview.prior_files} previously indexed files would disappear. Prior index generation ${preview.prior_generation}.`});
      const confirm=button('Confirm removals and reindex',async()=>{
        if(!owns())return;
        confirm.disabled=true;
        note.textContent='Confirming this removal preview…';
        try{
          await subscription.confirmRemovals(preview.confirmation);
          if(owns())closeRemovalReview();
        }catch{
          if(owns())removalReview.replaceChildren(el('p',{text:'The preview is no longer current or indexing is unavailable.'}),
            button('Refresh preview',()=>void openRemovalReview()),cancel);
        }
      });
      removalReview.replaceChildren(note,...(paths.length?
        [el('p',{text:'Sample paths'}),el('ul',{},...paths.map(path=>el('li',{text:path.slice(0,512)})))]:[]),confirm,cancel);
    }catch{
      if(owns())removalReview.replaceChildren(el('p',{text:'This removal preview is unavailable.'}),cancel);
    }
  }

  function closeTaskContributions(){
    taskContributionRead?.abort();taskContributionRead=null;taskContributionKey=null;
    taskContributionsPanel.hidden=true;taskContributionsPanel.replaceChildren();
  }

  async function showTaskContributions(){
    const scope=activityContext(),{workspaceID:workspaceId,chatID:chatId,receipt}=scope;
    if(!current()||!receipt||!chatId)return;
    closeTaskContributions();
    const controller=taskContributionRead=new AbortController();
    const key=taskContributionKey=JSON.stringify([workspaceId,chatId,codeReceiptKey(receipt)]);
    const owns=()=>current()&&taskContributionRead===controller&&!controller.signal.aborted&&key===taskContextKey();
    const close=button('Close task contributions',closeTaskContributions);
    taskContributionsPanel.hidden=false;
    taskContributionsPanel.replaceChildren(el('p',{text:'Reading task contributions…',role:'status'}),close);
    taskContributionsPanel.focus({preventScroll:true});
    try{
      const url=new URL('/api/orchestration/'+encodeURIComponent(chatId)+'/task-contributions',window.location.origin);
      url.searchParams.set('workspace_id',workspaceId);
      const response=await fetch(url,{credentials:'same-origin',signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error();
      const value=await response.json();
      if(!owns())return;
      if(value?.version!==1||value.coverage!=='partial_task_progress'||!Array.isArray(value.items)||value.items.length>20)throw new Error();
      const states={reported:'Reported',committed:'Committed',not_applied:'Not applied',conflict:'Conflict',unknown:'Outcome unknown'};
      const id=value=>typeof value==='string'&&value.length>0&&value.length<=256;
      const seen=new Set(), rows=[];
      for(const row of value.items){
        const task=row?.taskRef;
        if(!id(task?.sessionID)||!id(task?.taskID)||!Number.isSafeInteger(task?.assignmentEventID)||task.assignmentEventID<1||
            !id(row.actorID)||!Number.isSafeInteger(row.executionRevision)||!Number.isSafeInteger(row.observedAt)||
            !Object.hasOwn(states,row.state)||!/^[0-9a-f]{64}$/.test(row.witnessSHA256||''))continue;
        const identity=JSON.stringify([task.sessionID,task.taskID,task.assignmentEventID,row.actorID,row.executionRevision,row.messageID,row.operationID,row.toolCallID,row.state]);
        if(seen.has(identity))continue;
        seen.add(identity);
        const observed=new Date(row.observedAt);
        const parts=[task.taskID,row.actorID+' · revision '+row.executionRevision,states[row.state],
          Number.isFinite(observed.valueOf())?observed.toLocaleString():null];
        const rowLabel=el('li',{text:parts.filter(Boolean).join(' · ')});
        const source=row.source;
        if(row.state==='committed'&&row.sourceAvailable===true&&typeof source?.resourceRef==='string'&&
            source.resourceRef.length>0&&source.resourceRef.length<=16384&&source.revision?.kind==='hostFingerprint'&&
            /^sha256:[0-9a-f]{64}:(?:0|[1-9][0-9]{0,5})$/.test(source.revision.value)){
          const sourceStatus=el('span',{role:'status'});
          rowLabel.append(' ',button('Open committed report',async()=>{
            if(!owns())return;
            try{
              const {openResource}=await import('../copal.js');
              if(owns())await openResource(source.resourceRef,{expectedRevision:source.revision,isCurrent:owns});
            }catch{if(owns())sourceStatus.textContent='The committed report changed or is unavailable.';}
          }),sourceStatus);
        }
        rows.push(rowLabel);
      }
      const rendered=rows.length?el('ol',{},...rows):el('p',{text:'No qualified task contributions were available.'});
      taskContributionsPanel.replaceChildren(el('p',{text:'Task contributions · partial recorded progress'}),rendered,close);
    }catch{
      if(owns())taskContributionsPanel.replaceChildren(el('p',{text:'Task contributions are unavailable for this chat and workspace.'}),close);
    }
  }

  function taskContextKey(){
    const {workspaceID,chatID,receipt}=activityContext();
    return JSON.stringify([workspaceID,chatID,codeReceiptKey(receipt)]);
  }

  function qualifiedReceipt(){
    return activityContext().receipt;
  }

  function geometryLayout(){
    return presentation==='map' ? 'map' : JSON.stringify([presentation,settings.grouping,settings.groupDepth]);
  }

  function mapGeometry(nodes, receipt=qualifiedReceipt()){
    const saved=[],pinned=[];
    if(!receipt)return {saved,pinned};
    const stored=geometry.read(receipt,geometryLayout()), canonical=new Map(mapData.nodes.map(node=>[node.id,node.resource_id]));
    for(const node of nodes){
      const point=stored.get(canonical.get(node.id));
      if(!point)continue;
      const local={id:node.id,x:point.x,y:point.y};
      saved.push(local);
      if(point.pinned)pinned.push(local);
    }
    return {saved,pinned};
  }

  function rememberSceneGeometry(value){
    if(value.status!=='ready'||!scene||!renderedProjection||value.namespace!==codeMapKey(mapData.authority))return;
    const receipt=qualifiedReceipt();
    if(!receipt)return;
    const stamp=JSON.stringify([sceneKey,geometryLayout(),value.layout?.latest]);
    if(stamp===geometryStamp)return;
    geometryStamp=stamp;
    const canonical=new Map(mapData.nodes.map(node=>[node.id,node.resource_id]));
    geometry.remember(receipt,geometryLayout(),(scene.settledPositions?.()||[]).flatMap(point=>{
      const id=canonical.get(point.id);
      return id?[{...point,id}]:[];
    }));
    if(!pinButton.hidden&&selected)paintSelection();
  }

  function togglePin(){
    const receipt=qualifiedReceipt(), binding=model.bindingFor(selected);
    const point=scene.settledPositions?.()?.find(item=>item.id===selected);
    if(!receipt||!binding||!point)return;
    if(geometry.togglePin(receipt,geometryLayout(),{...point,id:binding.resource_id})){
      scene.setPins?.(mapGeometry(renderedProjection?.nodes||[],receipt).pinned);
      paintSelection();
    } else selectionNotice.textContent='The position could not be saved. Check browser storage or unpin another item.';
  }

  function clearEvidence(){
    evidenceRead?.dispose();evidenceRead=null;
    traceStart=null;traceResult=null;activeFlow=null;
    pathPanel.hidden=true;flowPanel.hidden=true;clearTraceButton.hidden=true;
    pathPanel.replaceChildren();flowPanel.replaceChildren();
    scene?.setPath?.([]);
  }

  function beginTrace(){
    const binding=model.bindingFor(selected),receipt=qualifiedReceipt();
    if(!binding||!receipt)return;
    clearEvidence();
    stopProgression();
    traceStart={id:binding.resource_id,label:mapData.nodes.find(node=>node.id===selected)?.label||'Selection',receipt};
    clearTraceButton.hidden=false;
    pathPanel.hidden=false;
    pathPanel.append(el('p',{text:'Choose the second endpoint in the Graph, Explorer or Table.'}));
    pathPanel.focus({preventScroll:true});
  }

  function witnessSourceRange(span,sourceHash){
    return span&&Number.isSafeInteger(span.start_byte)&&Number.isSafeInteger(span.end_byte)&&
      span.start_byte>=0&&span.end_byte>span.start_byte&&/^[0-9a-f]{64}$/.test(sourceHash||'')
      ?{startByte:span.start_byte,endByte:span.end_byte,sourceHash}:undefined;
  }

  function exactSource(id,receipt,stillCurrent,sourceRange){
    if(!stillCurrent()||!onEvidenceAction)return;
    const signal=actionAborter.signal;
    void onEvidenceAction('open',{node:{id,kind:id.startsWith('occ:')?'occurrence':'declaration'},receipt,workspaceId:workspace,isCurrent:stillCurrent,signal,sourceRange})
      .then(result=>{if(stillCurrent()&&result?.message)selectionNotice.textContent=result.message;})
      .catch(()=>{if(stillCurrent())selectionNotice.textContent='Current source unavailable.';});
  }

  function readScopedEvidence(query, receipt) {
    evidenceRead?.dispose();
    const owns=()=>current()&&codeReceiptKey(qualifiedReceipt())===codeReceiptKey(receipt)&&evidenceRead===reader;
    const reader=createCodeGraphModel({request,isCurrent:owns,limits:{paths:6,nodes:64,edges:64}});
    evidenceRead=reader;
    return reader.run(workspace,{
      schema:'open-clank.code-query/v1',expected_receipt:receipt,fields:['label','source','properties'],
      budgets:{rows:64,hops:settings.searchHops,bytes:settings.searchKiB*1024,tokens:settings.searchTokens,work:settings.queryWork},...query,
    }).then(ok=>owns()?{ok,result:reader.snapshot(),owns}:null);
  }

  async function traceTo(binding){
    const start=traceStart;
    if(!start||codeReceiptKey(binding.receipt)!==codeReceiptKey(start.receipt))return;
    traceResult={pending:true};
    pathPanel.replaceChildren(el('p',{text:'Reading witnessed outgoing paths…'}));
    const value=await readScopedEvidence({operation:'path',seed_ids:[start.id],target_id:binding.resource_id,direction:'outgoing'},start.receipt);
    if(!value||traceStart!==start)return;
    const {ok,result,owns}=value;
    const paths=ok ? result.paths.filter(path=>path.node_ids.length>=2 && path.node_ids[0]===start.id
      && path.node_ids.at(-1)===binding.resource_id && path.edges.length===path.node_ids.length-1
      && path.edges.every((edge,index)=>edge.from===path.node_ids[index]&&edge.to===path.node_ids[index+1])) : [];
    traceResult={paths,receipt:codeReceiptKey(start.receipt)};
    const names=new Map([...mapData.nodes.map(node=>[node.resource_id,node.label]),...result.nodes.map(node=>[node.id,node.label])]);
    const output=[el('p',{text:paths.length
      ? `${paths.length} outgoing path${paths.length===1?'':'s'} from ${start.label} · ${result.partial?'partial static evidence':'indexed static evidence'}`
      : ok?'No witnessed outgoing path within the selected search budget.':'Path evidence is unavailable.'})];
    for(const path of paths){
      output.push(el('ol',{},...path.edges.map(edge=>el('li',{},
        el('span',{text:`${names.get(edge.from)||'Source'} → ${names.get(edge.to)||'Target'} · ${edge.relation_kind} · ${edge.resolution_state} `}),
        button('Open witness',()=>exactSource(edge.occurrence_id,start.receipt,owns,witnessSourceRange(edge.source?.span,edge.source?.source_hash)))))));
    }
    pathPanel.replaceChildren(...output);syncTracedEdges();
  }

  function syncTracedEdges(){
    const byCanonical=new Map(mapData.nodes.map(node=>[node.resource_id,node.id]));
    const edges=(traceResult?.receipt&&traceResult.receipt===codeReceiptKey(mapData.authority?.receipt)?traceResult.paths:[]).flatMap(path=>path.edges.flatMap(edge=>{
      const from=byCanonical.get(edge.from),to=byCanonical.get(edge.to);
      return from&&to?[{from,to,relation:edge.relation_kind}]:[];
    }));
    scene?.setPath?.(edges);
  }

  async function showFlow(){
    const visual=selected,binding=model.bindingFor(visual),receipt=qualifiedReceipt();
    if(!receipt||!binding?.resource_id.startsWith('decl:'))return;
    stopProgression();
    activeFlow={visual}; const initiated=activeFlow;
    flowPanel.hidden=false;
    flowPanel.replaceChildren(el('p',{text:'Reading function flow…'}));
    flowPanel.focus({preventScroll:true});
    const value=await readScopedEvidence({operation:'inspect',seed_ids:[binding.resource_id]},receipt);
    if(!value||activeFlow!==initiated||selected!==visual)return;
    const hit=value.result.nodes.find(node=>node.id===binding.resource_id);
    const flow=hit?.properties?.functionFlow;
    if(flow?.version!==1||!['python_lexical_flow_v1','javascript_lexical_flow_v1'].includes(flow.profile)||flow.status!=='complete'
      ||!Array.isArray(flow.blocks)||flow.blocks.length>128||!Array.isArray(flow.edges)||flow.edges.length>256){
      flowPanel.replaceChildren(el('p',{text:flow?.status==='unsupported'?'This function is outside the available lexical-flow profile.':'Function flow evidence is unavailable for this index.'}));
      return;
    }
    const source=hit?.source, outer=source?.span;
    const owns=()=>value.owns()&&selected===visual&&activeFlow===initiated;
    const blocks=new Map(flow.blocks.filter(block=>Number.isSafeInteger(block.id)&&Number.isSafeInteger(block.span?.start_line)
      &&Number.isSafeInteger(block.span?.end_line)&&block.span.start_line>0&&block.span.end_line>=block.span.start_line).map(block=>[block.id,block]));
    const rows=flow.edges.filter(edge=>blocks.has(edge.from)&&blocks.has(edge.to)&&['next','true','false','return','raise'].includes(edge.kind));
    const blockSource=block=>{
      const span=block.span, line=span.start_line;
      const label=block.kind==='exit'?'Function exit':line===span.end_line?`Line ${line}`:`Lines ${line}–${span.end_line}`;
      const range=witnessSourceRange(span,source?.source_hash);
      if(!onEvidenceAction||!capability.available||!range||!(outer?.start_byte<=span.start_byte&&span.end_byte<=outer?.end_byte))
        return el('span',{text:label});
      return button(label,()=>exactSource(binding.resource_id,receipt,owns,range),
        {'aria-label':`Open ${block.kind} at ${label.toLowerCase()} in Editor`});
    };
    flowPanel.replaceChildren(el('p',{text:`${flow.profile.startsWith('python')?'Python':'JavaScript/TypeScript'} lexical flow · indexed source spans`}),
      el('ol',{},...rows.map(edge=>el('li',{},blockSource(blocks.get(edge.from)),el('span',{text:` → ${edge.kind} → `}),blockSource(blocks.get(edge.to))))),
      button('Open declaration',()=>exactSource(binding.resource_id,receipt,owns)));
  }

  function branchRequest(id,operation='children'){
    return {schema:MAP_SCHEMA,operation: id===anchor&&operation==='children'?'overview':operation,
      root:id===anchor&&operation==='children'?null:{kind:'visual',id},direction:operation==='neighbors'?connectionDirection:'both',expected_authority:copy(mapData.authority),limits:readLimits()};
  }
  function loadMap(operation,{root=null,more=false,expansionTicket=null,direction=operation==='neighbors'?connectionDirection:'both',relations:requestedRelations,signal,background=false}={}){
    const atEpoch=epoch,workspaceId=workspace;
    const task=async()=>{
      if(signal?.aborted||expansionTicket!==null&&expansionTicket!==expansionSerial)return false;
      if(!current()||!hostVisible||view!=='map'||atEpoch!==epoch||workspaceId!==workspace||!codeMapMayRead(index))return false;
      if(!background){actionSerial++;actionAborter.abort();actionAborter=new AbortController();}
      const owned={schema:MAP_SCHEMA,operation,root,direction,expected_authority:copy(mapData.authority),limits:readLimits(),
        ...(requestedRelations?{relations:requestedRelations}:{})};
      if(!background)query=owned;
      let result;
      deferProjectionPaint=background;
      try {result=await model.run(workspaceId,owned,{more,signal,background});}
      finally {deferProjectionPaint=false;}
      if(result&&!signal?.aborted&&(expansionTicket===null||expansionTicket===expansionSerial)&&root?.kind==='visual'){
        if(operation==='children'){
          if(background&&!expanded.has(root.id))automaticExpanded.add(root.id);
          if(!background)automaticExpanded.delete(root.id);
          expanded.add(root.id);
        }
        if(operation==='neighbors'){
          if(background&&!contexts.has(root.id))automaticContexts.add(root.id);
          if(!background)automaticContexts.delete(root.id);
          contexts.add(root.id);
        }
      }
      if(result&&background&&!signal?.aborted&&operation==='neighbors'&&root?.kind==='resource'
          &&mapData.selection?.resource_id===root.resource_id){
        const id=mapData.selection.visual_id;
        if(!contexts.has(id))automaticContexts.add(id);
        contexts.add(id);
      }
      if(result||background&&!signal?.aborted){updateScene();paint();}return result;
    };
    const pending=readQueue.then(task,task);readQueue=pending.catch(()=>false);return pending;
  }
  async function initialExpansion({signal,deferProgression=false}={}){
    stopProgression();
    const ticket=++expansionSerial;
    retainedIntent={expanded:[],hidden:[],contexts:[],selection:null};
    expanded.clear();contexts.clear();hiddenChildren.clear();automaticExpanded.clear();automaticContexts.clear();
    anchor=null;
    expansionNotice='Loading the folder overview…';
    paint();
    const loaded=await loadMap('overview',{expansionTicket:ticket,signal});
    if(ticket!==expansionSerial)return false;
    expansionNotice='';
    if(loaded&&!deferProgression)startProgression();
    paint();
    return loaded;
  }

  function stopProgression({keep=false}={}){
    if(!progression)return;
    progression.controller?.abort();
    if(keep) expansionNotice='Automatic loading paused.';
    else {progression=null;expansionNotice='';}
  }

  function startProgression(){
    if(!anchor||!mapData.authority)return;
    stopProgression();
    const published=mapData.authority.variant==='published';
    if(!published&&!mapData.diagnostics?.retainedSummaryBytes){
      expansionNotice='Automatic folder discovery requires a reusable inventory. Expand a folder to read it directly.';
      paint();
      return;
    }
    if(!published&&settings.nodeScope==='symbols'){
      expansionNotice='The symbols filter is waiting for indexed declarations.';
      paint();
      return;
    }
    const canEnter=id=>{
      if(hiddenChildren.has(id)||hiddenTerritories.has(id))return false;
      if(!hiddenChildren.size&&!hiddenTerritories.size&&!soloRoot)return true;
      const byId=new Map(mapData.nodes.map(node=>[node.id,node]));
      if(soloRoot&&id!==soloRoot&&!descendantOf(id,soloRoot,byId)&&!descendantOf(soloRoot,id,byId))return false;
      return [...hiddenChildren,...hiddenTerritories].every(hidden=>!descendantOf(id,hidden,byId));
    };
    const rootPage=model.pageFor(branchRequest(anchor));
    const planner=createCodeMapProgression({
      anchor,initialDepth:settings.initialDepth,readBudget:settings.autoReads,fileAtlas:published&&settings.nodeScope==='files',canEnter,
      wantSymbols:published&&settings.nodeScope!=='files'&&(settings.showFunctions||settings.showClasses||settings.showOtherSymbols),
      wantRelationships:published&&settings.maxEdges>0&&[...relations].some(relation=>relation!=='containment'),
    });
    planner.begin(mapData.nodes,rootPage);
    progression={planner,canEnter,published,representative:mapData.representative,authority:codeMapKey(mapData.authority),reads:1,first:true,stopReason:'',task:null,controller:null};
    runProgression();
  }

  function yieldProgression(signal,ms){
    return new Promise(resolve=>{
      const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',done);resolve();};
      const timer=setTimeout(done,ms);
      if(signal.aborted)done();
      else signal.addEventListener('abort',done,{once:true});
    });
  }

  function runProgression(){
    const owner=progression;
    if(!owner||owner.controller||surfaceView==='project'||!canLoadAutomatically())return;
    const controller=owner.controller=new AbortController(),signal=controller.signal;
    const owns=()=>progression===owner&&!signal.aborted&&surfaceView!=='project'&&canLoadAutomatically()&&owner.authority===codeMapKey(mapData.authority);
    void (async()=>{
      try {
        // Give incoming overview nodes one settled frame to seed locations.
        if(owner.first){
          for(let frame=0;frame<16&&owns()&&scene.snapshot().status!=='ready';frame++) await yieldProgression(signal,50);
          if(!owns())return;
          owner.first=false;
        }
        const exemplar=owner.representative;
        owner.representative=null;
        const nodeCeiling=Math.min(settings.autoNodes,settings.retainedNodes);
        const roomForPage=()=>nodeCeiling-mapData.nodes.length>=settings.pageNodes-1;
        if(owner.published&&exemplar?.declaration_id&&exemplar.direction&&relations.has(exemplar.relation)
            &&!hiddenChildren.size&&!hiddenTerritories.size&&!soloRoot
            &&settings.nodeScope!=='files'&&settings.maxEdges>0&&owner.reads<settings.autoReads&&roomForPage()){
          owner.reads++;
          await loadMap('neighbors',{root:{kind:'resource',resource_id:exemplar.declaration_id,receipt:mapData.authority.receipt},
            direction:exemplar.direction,relations:[exemplar.relation],signal,background:true});
          await yieldProgression(signal,45);
        }
        while(owns()){
          const task=owner.task??=owner.planner.next();
          if(!task){
            expansionNotice=owner.stopReason;
            progression=null;
            paint();
            return;
          }
          if(!owner.canEnter(task.id)){
            // The planner rechecks live branch visibility on its next pick.
            owner.task=null;
            continue;
          }
          const request=branchRequest(task.id,task.phase==='root'?'children':task.operation);
          const prior=model.pageFor(request);
          if(prior&&!task.more){
            if(task.phase==='folder'||task.phase==='declarations'){
              if(!expanded.has(task.id))automaticExpanded.add(task.id);
              expanded.add(task.id);
              updateScene();
            }
            owner.planner.accept(task,mapData.nodes,prior);
            owner.task=null;
            continue;
          }
          if(!mapData.nodes.some(node=>node.id===task.id)||task.more&&!prior?.next_cursor){
            owner.planner.accept(task,mapData.nodes,null,false);
            owner.stopReason='Some branch data left the cache. Expand a branch to read it again.';
            owner.task=null;
            continue;
          }
          const exhausted=owner.reads>=settings.autoReads?'Discovery reached the automatic read budget.':
            !roomForPage()?'The next page could exceed the loaded-node budget.':
            mapData.evicted||mapData.retainedBytes>=settings.cacheMiB*1048576?'Discovery reached the retention budget.':'';
          if(exhausted){
            expansionNotice=`${exhausted} Expand a branch or choose Load more.`;
            progression=null;
            paint();
            return;
          }
          const loading=owner.published?'Discovering indexed code':'Discovering folders and files';
          expansionNotice=`${loading} · ${owner.reads} of ${settings.autoReads} reads`;
          paint();
          owner.reads++;
          const ok=await loadMap(request.operation,{root:request.root,more:task.more,direction:request.direction,signal,background:true});
          if(!owns())return;
          if(!ok&&['stale_cursor','denied','erased','reindex_required'].includes(mapData.error?.code)){
            expansionNotice=owner.published?'The indexed scope changed. Refresh the map to continue.':'The folder inventory changed. Refresh the map to continue.';
            progression=null;
            paint();
            return;
          }
          if(!ok)owner.stopReason=mapData.error?.code==='budget_exceeded'
            ?'A branch exceeded the page budget. Adjust Graph limits or expand a different branch.'
            :'Some branches could not be read. Expand a branch to continue.';
          owner.planner.accept(task,mapData.nodes,model.pageFor(request),ok);
          owner.task=null;
          await yieldProgression(signal,45);
        }
      } finally {
        if(progression===owner&&owner.controller===controller){
          owner.controller=null;
          if(signal.aborted)runProgression();
        }
      }
    })();
  }

  async function expandBranch(id,{more=false}={}){
    const ticket=++expansionSerial;expansionBusy=false;stopRequestedFocus();const node=mapData.nodes?.find(n=>n.id===id);if(!node||!expandable(node))return false;
    retainedIntent.hidden=retainedIntent.hidden.filter(n=>intentKey(n)!==intentKey(nodeIntent(id)));hiddenChildren.delete(id);automaticExpanded.delete(id);expanded.add(id);const r=branchRequest(id);const page=model.pageFor(r);
    if(!more&&page?.complete){updateScene();paint();return true;}
    const result=await loadMap(r.operation,{root:r.root,more,expansionTicket:ticket});updateScene();paint();return result;
  }
  function collapseBranch(id){
    const node=mapData.nodes?.find(n=>n.id===id);if(!node||!expandable(node))return;
    stopRequestedFocus({keepProgression:true});expansionSerial++;expansionBusy=false;hiddenChildren.add(id);const descriptor=nodeIntent(id);if(descriptor&&!retainedIntent.hidden.some(n=>intentKey(n)===intentKey(descriptor)))retainedIntent.hidden.push(descriptor);retainedIntent.expanded=retainedIntent.expanded.filter(n=>descriptor&&intentKey(n)!==intentKey(descriptor)&&!(descriptor.kind!=='symbol'&&descriptor.path.every((part,i)=>n.path[i]===part)));expanded.delete(id);const byId=new Map((mapData.nodes||[]).map(n=>[n.id,n]));
    for(const key of [...expanded,...contexts]){let at=byId.get(key),seen=new Set();while(at?.parent&&!seen.has(at.id)){seen.add(at.id);if(at.parent===id){expanded.delete(key);contexts.delete(key);break;}at=byId.get(at.parent);}}
    contexts.delete(id);updateScene();paint();runProgression();
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
      expanded.add(id);const r=branchRequest(id);let page=model.pageFor(r);
      if(!page?.complete){
        do {
          if(!await loadMap(r.operation,{root:r.root,more:Boolean(page?.next_cursor),expansionTicket:ticket}))return false;
          reads++;page=model.pageFor(r);
        }while(settings.nodeScope==='files'&&page?.next_cursor&&ticket===expansionSerial&&reads<settings.autoReads&&
          mapData.nodes.length<Math.min(settings.autoNodes,settings.retainedNodes));
      }
      if(ticket!==expansionSerial)return false;
      if(page?.next_cursor)limited=true;
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
  async function connect(id){stopProgression();if(mapData.authority?.variant!=='published')return false;const ticket=++expansionSerial;expansionBusy=false;return loadMap('neighbors',{root:{kind:'visual',id},expansionTicket:ticket});}
  function loadBranchMore(){stopProgression();const ticket=++expansionSerial;expansionBusy=false;const node=mapData.nodes.find(n=>n.id===selected),id=node?.id||anchor;if(!id)return false;const r=branchRequest(id,['file','symbol'].includes(node?.kind)?'neighbors':'children');return loadMap(r.operation,{root:r.root,more:true,expansionTicket:ticket,direction:r.direction});}
  function select(id,{requested=false,restore=false}={}){
    if(!current())return;const node=mapData.nodes?.find(n=>n.id===id);if(!node)return;
    const binding=model.bindingFor(id);
    if(traceStart&&binding&&binding.resource_id!==traceStart.id&&!traceResult)void traceTo(binding);
    if(activeFlow&&activeFlow.visual!==id){activeFlow=null;flowPanel.hidden=true;}
    if(!requested){retainedIntent.selection=nodeIntent(id);focusSerial++;focusPath='';focusMessage='';}
    const receipt=qualifiedReceipt();
    if(receipt&&!restore)geometry.rememberFocus(receipt,binding?.resource_id||null);
    expansionSerial++;expansionBusy=false;selected=id;actionSerial++;actionAborter.abort();actionAborter=new AbortController();scene.select(id);togglePanel('inspector',true);paintList();paintSelection();
    if(!requested&&!traceStart&&['file','symbol'].includes(node.kind)&&mapData.authority?.variant==='published')void connect(id);
  }
  function resetProjectOverview(){
    projectRequest?.abort();projectRequest=null;projectPage=null;projectAuthority=null;projectTrail=[];projectCursor=null;projectError='';projectTotals=null;projectRegion=null;projectParents=[];projectAutoAuthority=null;
  }
  function activeProjectAuthority(){
    const authority=mapData.authority;
    if(authority?.variant==='structure_preview'&&!index.head_generation&&codeMapMayRead(index)&&authority.workspace_id===workspace&&
        ['workspace_revision','registration_id','checkout_id','admission_epoch','policy_generation'].every(key=>index[key]==null||index[key]===authority[key]))return authority;
    return qualifiedReceipt()&&authority?.variant==='published'?authority:null;
  }
  async function readProjectPage(cursor,trail,region=projectRegion,parents=projectParents){
    const authority=activeProjectAuthority();
    if(!authority||!current()||!hostVisible||surfaceView!=='project'||view!=='map'||cursor===undefined||projectRequest)return false;
    const key=codeMapKey(authority),workspaceId=workspace,ticket=epoch,controller=projectRequest=new AbortController();
    const owns=()=>current()&&epoch===ticket&&workspaceId===workspace&&projectRequest===controller&&!controller.signal.aborted&&
      surfaceView==='project'&&view==='map'&&key===codeMapKey(activeProjectAuthority());
    const query=mapRequest({schema:MAP_SCHEMA,operation:region?'children':'overview',root:region?{kind:'visual',id:region.id}:null,expected_authority:copy(authority),cursor,
      relations:['containment'],limits:{...readLimits(),nodes:Math.min(150,settings.pageNodes),groups:Math.min(40,settings.pageGroups)}});
    projectError='';paintProjectOverview();
    try {
      const data=validateMapResult(await requestMap(workspaceId,{mode:'map',request:query},controller.signal),query);
      if(!owns())return false;
      if(!data.root||data.page.next_cursor&&[cursor,...trail].includes(data.page.next_cursor))throw Error('Project page did not advance');
      projectAuthority=key;projectPage=data;projectTrail=trail;projectCursor=cursor;projectRegion=region;projectParents=parents;
      if(!region&&!cursor)projectTotals=data.territories?.find(row=>row.visual_id===data.root)??null;
      return true;
    } catch {
      if(owns())projectError='This overview page is unavailable. Start at the first page to refresh.';
      return false;
    } finally {
      if(projectRequest===controller){projectRequest=null;paintProjectOverview();}
    }
  }
  async function enterProjectItem(node){
    const authority=activeProjectAuthority();
    if(!authority||projectAuthority!==codeMapKey(authority))return;
    const key=projectAuthority,workspaceId=workspace;
    stopProgression();stopRequestedFocus();
    switchSurface('canvas',{resume:false});
    const ticket=++expansionSerial;
    const ok=await loadMap('children',{root:{kind:'visual',id:node.id},expansionTicket:ticket});
    if(!current()||workspace!==workspaceId||ticket!==expansionSerial||key!==codeMapKey(activeProjectAuthority()))return;
    if(!ok||!mapData.nodes.some(row=>row.id===node.id)){
      switchSurface('project');projectError='The selected item could not be loaded. Refresh the overview and try again.';paintProjectOverview();return;
    }
    soloRoot=node.id;
    select(node.id,{requested:true});
    updateScene();paint();scene.fit?.();
  }
  function paintProjectOverview(){
    if(surfaceView!=='project'||view!=='map')return;
    const authority=activeProjectAuthority(),key=authority?codeMapKey(authority):null;
    if(projectAuthority&&projectAuthority!==key)resetProjectOverview();
    if(authority&&!projectPage&&!projectRequest&&!projectError&&projectAutoAuthority!==key){
      projectAutoAuthority=key;
      queueMicrotask(()=>{if(projectAutoAuthority===key&&!projectPage&&!projectRequest)void readProjectPage(null,[]);});
    }
    const focus=projectRows.contains(document.activeElement)?document.activeElement.dataset.projectId:null;
    const page=projectPage?.page;
    const root=projectPage?.nodes.find(node=>node.id===projectPage?.root);
    const format=value=>Number.isSafeInteger(value)?value.toLocaleString():'—';
    const preview=projectPage?.authority.variant==='structure_preview';
    const total=projectRegion?root?.source_files:(projectTotals?.source_files??root?.source_files);
    const scope=projectRegion?`${root?.label||projectRegion.label} · `:'';
    projectSummary.textContent=root?`${scope}${format(total)} ${preview?'observed':'indexed'} source files · ${format(root.asset_files)} assets${!projectRegion&&projectTotals?' · '+format(projectTotals.declarations)+' indexed declarations · '+format(projectTotals.observed_references)+' observed references':''}. ${preview?'Structure preview while indexing.':projectPage.coverage.inventory_complete?'Index inventory reported complete.':'Index inventory is partial.'}`:'Waiting for checkout structure.';
    projectStatus.textContent=projectRequest?'Reading folder structure…':projectError||(page?`${page.remaining_nodes==null?'':format(page.remaining_nodes)+' items remaining in this folder. '}Open an item to explore its graph.`:'');
    projectTop.hidden=!projectRegion;projectTop.disabled=!!projectRequest;
    projectUp.hidden=!projectParents.length;projectUp.disabled=!!projectRequest;
    projectStart.disabled=!authority||!!projectRequest;
    projectPrevious.disabled=!projectTrail.length||!!projectRequest;
    projectNext.disabled=!page?.next_cursor||!!projectRequest;
    if(projectRendered===projectPage)return;
    projectRendered=projectPage;
    const rows=projectPage?.nodes.filter(node=>node.parent===projectPage.root&&['file','folder'].includes(node.kind))??[];
    projectRows.replaceChildren(...rows.map(node=>el('tr',{},el('th',{scope:'row'},button(node.label,()=>void enterProjectItem(node),{'data-project-id':node.id})),
      el('td',{text:node.kind}),el('td',{text:format(node.source_files)}),el('td',{text:format(node.asset_files)}),
      el('td',{},...(node.kind==='folder'?[button('Browse',()=>{
        const parents=[...projectParents,{region:projectRegion,cursor:projectCursor,trail:projectTrail}].slice(-128);
        void readProjectPage(null,[],node,parents);
      },{'aria-label':`Browse ${node.label}`})]:[])))));
    if(focus)[...projectRows.querySelectorAll('[data-project-id]')].find(item=>item.dataset.projectId===focus)?.focus({preventScroll:true});
  }
  function switchSurface(next,{resume=true}={}){
    if(next!=='canvas')installEditorFilesStyles();
    if(next!=='project'&&projectRequest){projectRequest.abort();projectRequest=null;}
    if(next==='project')stopProgression({keep:true});
    surfaceView=next;
    viewport.hidden=next!=='canvas';
    tablePanel.hidden=next!=='table';projectPanel.hidden=next!=='project';
    showView('map');
    if(next==='table'){paintTable();tablePanel.focus({preventScroll:true});}
    else if(next==='project'){
      togglePanel('inspector',false);togglePanel('explorer',false);
      paintProjectOverview();projectPanel.focus({preventScroll:true});
      if(!projectPage&&!projectRequest)void readProjectPage(null,[]);
    }else{
      viewport.querySelector('canvas')?.focus({preventScroll:true});
      if(resume)runProgression();
    }
  }
  function showView(nextView){
    if(nextView!==view){expansionSerial++;expansionBusy=false;}if(nextView!=='map'&&projectRequest){projectRequest.abort();projectRequest=null;}view=nextView;projectButton.setAttribute('aria-pressed',String(view==='map'&&surfaceView==='project'));mapPanel.hidden=view!=='map';evidencePanel.hidden=view!=='evidence';mapButton.setAttribute('aria-pressed',String(view==='map'&&surfaceView==='canvas'));tableButton.setAttribute('aria-pressed',String(view==='map'&&surfaceView==='table'));evidenceButton.setAttribute('aria-pressed',String(view==='evidence'));
    syncVisibility();
    if(view==='evidence')ensureEvidence();
  }
  function syncVisibility(){
    const visible=hostVisible&&view==='map'&&!document.hidden;
    release?.setVisible?.(visible);
    if(visible){
      if(surfaceView==='canvas'){scene?.resume();resize();}
      else {stopFollowing();scene?.suspend();}
      startVisibleProjection();
    } else {
      stopProgression({keep:true});
      projectRequest?.abort();projectRequest=null;
      projectionStart?.abort();
      stopFollowing();
      scene?.suspend();
    }
  }
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
  async function applyQualifiedImpact(value, options={}) {
    if(!current() || value?.workspace_id!==workspace || !indexPresentation(index).usable || typeof options.isCurrent!=='function' || options.isCurrent()!==true) return false;
    showView('evidence');const active=ensureEvidence();await Promise.resolve();
    if(!current() || legacy!==active || options.isCurrent()!==true) return false;
    const applied=await active?.applyQualifiedImpact(value,options);
    if(applied&&current()&&value.recorded_after?.action_id&&value.recorded_after?.version_id&&value.resource_id&&
      codeReceiptKey(value.graph?.receipt)===codeReceiptKey(mapData.authority?.receipt)){
      changeContext={resourceID:value.resource_id,receipt:codeReceiptKey(value.graph.receipt),
        actionID:value.recorded_after.action_id,versionID:value.recorded_after.version_id};
    }
    return applied===true;
  }
  async function inspectSelectedEvidence(){
    const visual=selected, binding=model.bindingFor(visual), receipt=qualifiedReceipt();
    if(!current()||!binding||!receipt||codeReceiptKey(binding.receipt)!==codeReceiptKey(receipt))return false;
    showView('evidence');
    const active=ensureEvidence(), capturedWorkspace=workspace;
    const owns=()=>current()&&view==='evidence'&&legacy===active&&workspace===capturedWorkspace&&
      selected===visual&&codeReceiptKey(qualifiedReceipt())===codeReceiptKey(receipt);
    await Promise.resolve();
    return owns()&&active?.inspectResource({id:binding.resource_id,receipt,workspaceId:capturedWorkspace,isCurrent:owns});
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
    if(which==='explorer'&&!panel.hidden)paintList();
    revealSelection();
  }
  function revealSelection(){
    if(!scene||!selected||view!=='map'||followActor)return;
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
  function paintTable() {
    if(surfaceView!=='table')return;
    const focus=tableRows.contains(document.activeElement)?document.activeElement?.dataset.codeTableId:null;
    const projection=visibleMap(), rows=projection.nodes.slice().sort((a,b)=>a.label.localeCompare(b.label)||a.id.localeCompare(b.id));
    const degrees=new Map();
    for(const edge of projection.edges) {
      degrees.set(edge.from,(degrees.get(edge.from)||0)+1);
      if(edge.to!==edge.from)degrees.set(edge.to,(degrees.get(edge.to)||0)+1);
    }
    tablePage=Math.max(0,Math.min(tablePage,Math.ceil(rows.length/settings.explorerRows)-1));
    const start=tablePage*settings.explorerRows,end=Math.min(start+settings.explorerRows,rows.length);
    tableRows.replaceChildren(...rows.slice(start,end).map(node=>el('tr',{},
      el('th',{scope:'row'},button(node.label,()=>select(node.id),{'data-code-table-id':node.id,'aria-pressed':String(node.id===selected)})),
      el('td',{text:node.symbol_kind||node.kind}),el('td',{text:String(degrees.get(node.id)||0)}))));
    if(focus)[...tableRows.querySelectorAll('[data-code-table-id]')].find(row=>row.dataset.codeTableId===focus)?.focus({preventScroll:true});
    tablePrevious.disabled=tablePage===0;tableNext.disabled=end>=rows.length;
    tableStatus.textContent=rows.length?`${start+1}–${end} of ${rows.length} displayed items`:'No items match the active graph filters.';
  }
  function paintList(){
    if(explorer.hidden){paintTable();return;}
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
    paintTable();
    listLabel.textContent=`${rows.length?start+1:0}–${Math.min(rows.length,start+settings.explorerRows)} of ${rows.length} loaded`;
    previous.disabled=listPage===0;next.disabled=start+settings.explorerRows>=rows.length;const at=mapData.nodes.find(n=>n.id===selected),r=anchor?branchRequest(at?.id||anchor,['file','symbol'].includes(at?.kind)?'neighbors':'children'):null;more.disabled=mapData.status!=='ready'||!r||!model.pageFor(r)?.next_cursor;
  }
  function territoryCounts(row){
    return `${row.source_files} source files · ${row.declarations} indexed declarations · ${row.observed_references} observed references`;
  }
  function paintTerritories(visible){
    if(!budgetDetails.open)return;
    const nodes=new Map(visible.nodes.map(node=>[node.id,node]));
    const territories=(visible.territories||[]).filter(row=>nodes.has(row.visual_id)).slice(0,12);
    territorySummary.hidden=!territories.length;
    territorySummary.replaceChildren(...(territories.length?[
      el('strong',{text:'Indexed territory coverage'}),
      el('ul',{},...territories.map(row=>el('li',{},
        button(nodes.get(row.visual_id).label,()=>select(row.visual_id)),
        el('span',{text:' '+territoryCounts(row)})))),
    ]:[]));
  }
  function paintSelection(){
    const node=mapData.nodes?.find(n=>n.id===selected),selection=model.bindingFor(selected);
    inspectorTitle.textContent=node?.label||'Select a folder, file or relationship';
    inspectorSummary.textContent=node?`${node.symbol_kind||node.kind}${node.qualified_name?' · '+node.qualified_name:''} · ${node.source_files} source files · ${node.asset_files} assets${mapData.authority?.variant==='structure_preview'?' · structure only; not evidence':''}`:'';
    const territory=node&&mapData.territories?.find(row=>row.visual_id===node.id);
    if(territory)inspectorSummary.append(' · indexed territory · '+territoryCounts(territory));
    const showing=Boolean(node&&childrenShown(node.id));drill.textContent=showing?'Hide children':'Show children';drill.setAttribute('aria-expanded',String(showing));drill.hidden=!expandable(node);drill.disabled=!node||(!showing&&mapData.status!=='ready');neighbors.disabled=!node||mapData.status!=='ready'||mapData.authority?.variant!=='published';
    const canonical=selection?.visual_id===selected&&mapData.authority?.variant==='published'&&mapData.status==='ready'&&indexPresentation(index).usable;
    inspectEvidence.disabled=!canonical||!qualifiedReceipt()||codeReceiptKey(selection?.receipt)!==codeReceiptKey(qualifiedReceipt());
    const pinned=canonical&&qualifiedReceipt()&&geometry.read(qualifiedReceipt(),geometryLayout()).get(selection.resource_id)?.pinned===true;
    pinButton.disabled=!canonical||!qualifiedReceipt()||!scene.settledPositions?.()?.some(point=>point.id===selected);
    pinButton.textContent=pinned?'Unpin position':'Pin position';pinButton.setAttribute('aria-pressed',String(pinned));
    traceButton.disabled=!canonical||typeof request!=='function'||!qualifiedReceipt();
    flowButton.disabled=traceButton.disabled||node?.kind!=='symbol';
    recordedButton.disabled=!qualifiedReceipt()||typeof options.readRecordedActivity!=='function';
    if(recordedReceipt&&recordedReceipt!==codeReceiptKey(qualifiedReceipt()))clearRecordedActivity();
    taskContributionsButton.disabled=!qualifiedReceipt()||!activityContext().chatID;
    if(taskContributionKey&&taskContributionKey!==taskContextKey())closeTaskContributions();
    citationsButton.disabled=!canonical||!qualifiedReceipt()||typeof options.readDocumentEvidence!=='function';
    if(citationKey&&citationKey!==JSON.stringify([codeReceiptKey(qualifiedReceipt()),selection?.resource_id]))clearDocumentEvidence();
    if(traceStart&&codeReceiptKey(traceStart.receipt)!==codeReceiptKey(qualifiedReceipt()))clearEvidence();
    clearTraceButton.hidden=!traceStart;
    for(const control of evidenceActions)control.disabled=!canonical||!capability.available||!onEvidenceAction;
    const edges=(mapData.edges||[]).filter(e=>e.relation!=='containment'&&(!selected||e.from===selected||e.to===selected));
    edgeList.replaceChildren(el('strong',{text:`Relationships (${edges.length} loaded)`}),...edges.slice(0,settings.inspectorRows).map(e=>button(`${e.relation} · ${e.state} · ${e.count}${mapData.nodes.some(n=>(n.id===e.from||n.id===e.to)&&n.kind!=='file')?' · folder aggregate':''}`,()=>{
      stopProgression();
      void model.selectAggregate(e.selection_id);
    })));
    if(edges.length>settings.inspectorRows)edgeList.append(el('span',{text:`${edges.length-settings.inspectorRows} more loaded relationships are outside this inspector.`}));
    const aggregate=mapData.aggregate&&edges.some(e=>e.selection_id===mapData.aggregate.selection_id)?mapData.aggregate:null;referenceList.replaceChildren();
    if(aggregate){referenceList.append(el('strong',{text:`Underlying references (${aggregate.references.length}${aggregate.complete?'':', more remain'})`}),
      ...aggregate.references.map(r=>button(`${r.relation} · ${r.state} · inspect occurrence`,()=>evidence('neighbors',{seed_ids:[r.occurrence_id],direction:'both'}))));
      if(aggregate.next_cursor&&!aggregate.clientLimited)referenceList.append(button('More references',()=>{
        stopProgression();
        void model.selectAggregate(aggregate.selection_id,{cursor:aggregate.next_cursor});
      }));
    }
    const path=[],seen=new Set();let at=mapData.nodes?.find(n=>n.id===(selected||anchor));
    while(at&&!seen.has(at.id)&&path.length<64){path.unshift(at);seen.add(at.id);at=mapData.nodes.find(n=>n.id===at.parent);}
    breadcrumbs.replaceChildren(...path.map(n=>button(n.label,()=>{stopRequestedFocus();select(n.id);return n.kind==='file'?connect(n.id):expandBranch(n.id);} )));
  }
  function renderDiagnostics(){
    const receipt=mapData.authority?.receipt;
    const diagnosticText=JSON.stringify({
      index:codeMapIndexDiagnostics(index),
      map:{authority:mapData.authority,servedGeneration:receipt?.head_generation??null,
        latestReportedGeneration:index.head_generation??null,
        freshness:!receipt?'structure_only':receipt.head_generation!==index.head_generation?'previous_generation':index.freshness||'unverified',
        pageTiming:mapData.diagnostics??null,firstColdPageTiming:mapData.coldDiagnostics??null,coverage:mapData.coverage,page:mapData.page,error:mapData.error,
        evicted:mapData.evicted,retainedBytes:mapData.retainedBytes},
      renderer:rendererDiagnostic,
    },null,2);
    if(diagnostics.textContent!==diagnosticText)diagnostics.textContent=diagnosticText;
  }
  function paint(){
    paintActivity();
    if(!current())return;
    const visible=visibleMap();
    paintTerritories(visible);
    updateText(budgetText,Object.values(graphBudgetSummary(settings,mapData.coverage)).join(' ')
      + ` Loaded nodes outside this view: ${Math.max(0,mapData.nodes.length-visible.nodes.length)}. Page remaining nodes: ${mapData.page?.remaining_nodes ?? 'unknown'}; edges: ${mapData.page?.remaining_edges ?? 'unknown'}.`);
    updateText(status,[projectionStatus(),expansionNotice,surfaceView!=='project'&&mapData.evicted?'Exploration cache limited; revisit branches to reload.':'',surfaceView==='project'?'Project overview pages the index independently of the canvas.':`${visible.nodes.length} displayed / ${mapData.nodes.length} loaded nodes · ${visible.edges.length} displayed edges · effective total cap ${Math.min(settings.maxNodes,settings.retainedNodes)} (files ≤${Math.min(settings.maxFiles,settings.maxNodes,settings.retainedNodes)}, symbols ≤${Math.min(settings.maxSymbols,settings.maxNodes,settings.retainedNodes)})`,index.verified_at?'Last verified '+new Date(index.verified_at).toLocaleTimeString():'',focusMessage,sceneByteNotice,options.savedSourceNotice?'Graph represents saved/on-disk source; unsaved Editor changes were not saved or indexed.':''].filter(Boolean).join(' '));
    const preview=mapData.authority?.variant==='structure_preview';
    const failed=index.lifecycle==='failed',unavailable=['denied','erased','unavailable'].includes(index.lifecycle)||mapData.status==='error'||projectionFailure?.key===codeMapIndexKey(index);
    updateText(summary,(retryingOverview && projectionStart===retryingOverview && !retryingOverview.signal.aborted ? projectionStatus() : focusMessage)||(!workspace?'Choose a folder':unavailable?projectionStatus():[
      failed?'Indexing failed':preview?'Folder preview':'Source graph',
      `${visible.nodes.length.toLocaleString()} nodes`,
      !failed&&indexPresentation(index).pending?indexPhaseLabel(index.progress):''
    ].filter(Boolean).join(' · ')));
    options.onStatus?.(workspace ? projectionStatus() : '');status.dataset.recovering=String(index.recovering===true);
    groupingLabel.hidden=view!=='map'||presentation==='map';
    grouping.value=settings.grouping;
    grouping.querySelector('[value="workspaces"]').disabled=!mapData.nodes.some(n=>n.kind==='repository');
    restoreTerritory.hidden=!soloRoot&&!hiddenTerritories.size;
    const usable=indexPresentation(index).usable;search.disabled=searchButton.disabled=evidenceButton.disabled=!workspace||['denied','erased'].includes(index.lifecycle);
    projectButton.disabled=!activeProjectAuthority();paintProjectOverview();
    overview.disabled=!codeMapMayRead(index)||mapData.status==='loading';mapReload.disabled=!codeMapMayRead(index)||mapData.status==='loading';
    reviewRemovals.hidden=!removalReviewRequired();
    if(reviewRemovals.hidden&&!removalReview.hidden)closeRemovalReview();
    const needsRebuild = !indexPresentation(index).pending && mapData.authority?.variant === 'structure_preview'
      && Boolean(index.head_generation);
    recheck.disabled=!release;
    retry.hidden=!workspace||(!needsRebuild&&!['missing','failed','cancelled','unavailable'].includes(index.lifecycle)&&mapData.error?.code!=='reindex_required');
    retry.disabled=!workspace;readmit.hidden=index.lifecycle!=='erased';readmit.disabled=!release;detach.disabled=!release;
    empty.hidden=surfaceView==='project'||visible.nodes.length>0;
    empty.querySelector('span').textContent=activityScope==='changed'&&!recordedChangeNode()
      ?'Open a recorded comparison in History to locate its file.'
      : !workspace ? 'Choose a folder to display its code graph.'
      : expansionNotice || projectionStatus();
    renderDiagnostics();
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
  function codeNodeContextMenu(container, resolve) {
    const capture = (target, event) => {
      if (!current() || !hostVisible || view !== 'map' || mapData.status !== 'ready') return null;
      const hit = resolve(target, event);
      if (!hit) return null;
      const authority = codeMapKey(mapData.authority);
      const node = mapData.nodes.find(item => item.id === hit.id);
      return node && hit.authorityNamespace === authority ? { node, target:hit.target, authority, surface:surfaceView, epoch, workspace } : null;
    };
    const isCurrent = ({ adapterContext:saved }) => !!saved?.target.isConnected && container.contains(saved.target)
      && current() && hostVisible && view === 'map' && mapData.status === 'ready' && surfaceView === saved.surface
      && epoch === saved.epoch && workspace === saved.workspace && codeMapKey(mapData.authority) === saved.authority
      && mapData.nodes.includes(saved.node);
    const commands = ({ node }) => [
      { id:'inspect-code-node', label:'Inspect code node' },
      ...(sourceBinding(node.id) ? [{ id:'open-code-source', label:'Open source' }] : []),
    ];
    return registerAdapter(container, {
      editingCommands:false,
      hitTest:() => true,
      capture,
      isCurrent,
      commands:request => isCurrent(request) ? commands(request.adapterContext) : [],
      execute:(id, request) => {
        if (!isCurrent(request) || !commands(request.adapterContext).some(item => item.id === id)) return false;
        const nodeId = request.adapterContext.node.id;
        if (id === 'inspect-code-node') { select(nodeId); return true; }
        return act('open', nodeId, () => isCurrent(request));
      },
    });
  }

  const releaseCanvasMenu = codeNodeContextMenu(viewport, (target, event) => {
    if (target !== scene?.canvas || event.detail === 0 && !event.pointerType && !event.button && !event.clientX && !event.clientY) return null;
    const hit = scene.pickClient?.(event);
    return hit ? { ...hit, target } : null;
  });
  const releaseTableMenu = codeNodeContextMenu(tableRows, target => {
    const row = target.closest('tr');
    const node = row?.querySelector('button[data-code-table-id]');
    return node && tableRows.contains(node) ? { id:node.dataset.codeTableId, target:node, authorityNamespace:codeMapKey(mapData.authority) } : null;
  });
  const releaseExplorerMenu = codeNodeContextMenu(list, target => {
    const node = target.closest('button[data-code-map-id]');
    return node && list.contains(node) ? { id:node.dataset.codeMapId, target:node, authorityNamespace:codeMapKey(mapData.authority) } : null;
  });

  function sourceBinding(visual) {
    const binding = model.bindingFor(visual);
    const receipt = qualifiedReceipt();
    return binding?.visual_id === visual && receipt && codeReceiptKey(binding.receipt) === codeReceiptKey(receipt)
      && mapData.authority?.variant === 'published' && indexPresentation(index).usable && capability.available && onEvidenceAction ? binding : null;
  }

  async function act(kind, visual=selected, owns=()=>selected===visual){
    const selection=sourceBinding(visual);if(!current()||mapData.status!=='ready'||!selection||!owns())return;
    const receipt=copy(selection.receipt),id=selection.resource_id,workspaceId=workspace,authority=codeMapKey(mapData.authority),ticket=++actionSerial;
    const valid=()=>current()&&ticket===actionSerial&&workspace===workspaceId&&owns()&&sourceBinding(visual)?.resource_id===id&&codeMapKey(mapData.authority)===authority;
    for(const b of evidenceActions)b.disabled=true;
    try{
      let sourceRange;
      if(kind==='open'&&id.startsWith('decl:')){
        const use=await readScopedEvidence({operation:'inspect',seed_ids:[id]},receipt);
        if(!valid())return;
        const source=use?.ok?use.result.nodes.find(value=>value.id===id)?.source:null;
        sourceRange=witnessSourceRange(source?.span,source?.source_hash);
      }
      const result=await onEvidenceAction?.(kind,{node:{id,kind:id.startsWith('file:')?'file':'declaration'},receipt,workspaceId,isCurrent:valid,signal:actionAborter.signal,sourceRange});
      if(valid()){status.textContent=result?.message||'Source action completed.';if(result?.ok&&kind==='open')scene.activity?.([visual]);}
    }
    catch{if(valid())status.textContent='Current source action unavailable.';}
    finally{if(current() && ticket===actionSerial)paintSelection();}
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
  function menuCapture(){
    const node=mapData.nodes.find(row=>row.id===selected);
    const root=node?.id||anchor;
    const operation=node&&['file','symbol'].includes(node.kind)?'neighbors':'children';
    const continuation=root?model.pageFor(branchRequest(root,operation))?.next_cursor:null;
    return {workspace,epoch,view,presentation,layoutPaused,selected,connectionDirection,grouping:settings.grouping,activityScope,soloRoot,hiddenTerritories:[...hiddenTerritories].sort(),relations:[...relations].sort().join(','),authority:codeMapKey(mapData.authority||null),root,
    page:continuation||null,automaticContext:preference.checked,preferencePending:preference.disabled,status:mapData.status||'idle',index:codeMapIndexKey(index),expansion:expansionSerial,childrenShown:selected?childrenShown(selected):false,
    evidence:view==='evidence'&&legacy?JSON.stringify(legacy.menuCapture?.()||null):null};}
  // Loading/progress changes invalidate captured evidence, not harmless
  // presentation commands. Both paths still bind the same mounted folder/epoch.
  function menuScopeCurrent(c){return current()&&c&&c.workspace===workspace&&c.epoch===epoch;}
  function menuIsCurrent(c){return menuScopeCurrent(c)&&codeMapKey(c)===codeMapKey(menuCapture());}

  function showExportPreview(c) {
    if (!menuIsCurrent(c) || view !== 'map' || !renderedProjection) return;
    const build = () => codeGraphExportSnapshot(renderedProjection, {
      presentation, positions:scene?.snapshot().namespace===codeMapKey(renderedProjection.authority)?scene.settledPositions?.():null,
      direction:settings.direction, relations:[...relations], nodeScope:settings.nodeScope, showFunctions:settings.showFunctions, showClasses:settings.showClasses, showOtherSymbols:settings.showOtherSymbols,
      nodeLimit:Math.min(settings.maxNodes,settings.retainedNodes), edgeLimit:Math.min(settings.maxEdges,settings.retainedEdges), fileLimit:Math.min(settings.maxFiles,settings.maxNodes,settings.retainedNodes), symbolLimit:Math.min(settings.maxSymbols,settings.maxNodes,settings.retainedNodes),
      freshness:mapData.authority?.variant==='published' && index.head_generation && mapData.authority.receipt?.head_generation !== index.head_generation ? 'stale' : index.freshness === 'partial' ? 'partial' : mapData.authority?.variant==='published' && index.head_generation && indexPresentation(index).usable && !refreshing ? 'current' : 'unknown',
      remainingNodes:mapData.page?.remaining_nodes, remainingEdges:mapData.page?.remaining_edges,
      omittedNodes:Math.max(0,(mapData.nodes?.length || 0)-renderedProjection.nodes.length),
      omittedEdges:Math.max(0,(mapData.edges?.length || 0)-renderedProjection.edges.length),
      incomplete:mapData.page?.complete !== true || mapData.page?.truncated !== false || mapData.evicted === true || index.coverage_complete !== true,
    });
    const snapshot=build(), frozen=codeGraphExportJSON(snapshot), stale=snapshot.receipt.freshness==='stale';
    const details=el('p',{text:`${stale?'This receipt is stale. Refresh the graph before exporting. ':''}${snapshot.nodes.length} displayed nodes, ${snapshot.edges.length} relationships. ${snapshot.omissions.nodes} loaded nodes and ${snapshot.omissions.edges} loaded relationships omitted. Source text, absolute paths, account and activity fields are excluded. Coverage may be incomplete. SVG and Canvas use ${snapshot.layout ? 'the settled '+presentation+' positions' : 'a schematic grid'}; SVG shortens labels. Cypher is data for review.`});
    const save=(format)=>{
      if (stale || !menuIsCurrent(c) || !renderedProjection || codeGraphExportJSON(build()) !== frozen) { exportPanel.replaceChildren(el('p',{text:'Graph changed. Open a fresh export preview.'})); return; }
      const html=format==='html', graphml=format==='graphml', svg=format==='svg', canvas=format==='canvas', cypher=format==='cypher', markdown=format==='markdown';
      const blob=new Blob([html?codeGraphExportHTML(snapshot):graphml?codeGraphExportGraphML(snapshot):svg?codeGraphExportSVG(snapshot):canvas?codeGraphExportCanvas(snapshot):cypher?codeGraphExportCypher(snapshot):markdown?codeGraphExportMarkdown(snapshot):frozen],{type:html?'text/html;charset=utf-8':graphml?'application/graphml+xml;charset=utf-8':svg?'image/svg+xml;charset=utf-8':cypher||markdown?'text/plain;charset=utf-8':'application/json;charset=utf-8'});
      const url=URL.createObjectURL(blob), link=document.createElement('a');link.href=url;link.download=html?'code-graph.html':graphml?'code-graph.graphml':svg?'code-graph.svg':canvas?'code-graph.canvas':cypher?'code-graph.cypher':markdown?'code-graph.md':'code-graph.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),0);
      exportPanel.hidden=true;
    };
    const format=el('select',{'aria-label':'Export format'},...[["json","JSON"],["html","Offline HTML"],["graphml","GraphML"],["svg",snapshot.layout?"SVG (settled layout)":"SVG (schematic grid)"],["canvas",snapshot.layout?"Canvas (settled layout)":"Canvas (schematic grid)"],["cypher","Cypher data"],["markdown","Markdown"]].map(([value,text])=>el('option',{value,text})));
    const createWiki=button('Create new wiki document…',()=>{
      const valid=()=>!stale && menuIsCurrent(c) && !!renderedProjection && codeGraphExportJSON(build())===frozen;
      if (!valid()) { exportPanel.replaceChildren(el('p',{text:'Graph changed. Open a fresh export preview.'})); return; }
      options.createWikiDocument?.(codeGraphExportMarkdown(snapshot), valid);
    },{disabled:stale || typeof options.createWikiDocument!=='function'});
    exportPanel.replaceChildren(el('h3',{text:'Export current graph'}),details,format,button('Save',()=>save(format.value),{disabled:stale}),createWiki,button('Cancel',()=>{exportPanel.hidden=true;}));
    exportPanel.hidden=false;
  }

  function menuCommands(c){
    if(!menuIsCurrent(c))return [];
    const presentationCommands=new Set(['choose','map','table','evidence','explorer','inspector','fit','zoom-in','zoom-out','settings','layout-pause','settle','diagnostics']);
    const cmd=(id,label,menu,run,disabledReason='',checked)=>({id:'graph.'+id,label,menu,run,disabledReason,
      current:()=>presentationCommands.has(id)||id.startsWith('relation-')||id.startsWith('presentation-')
        ?menuScopeCurrent(c)&&c.view===view&&c.presentation===presentation
        :menuIsCurrent(c),...(typeof checked==='boolean'?{checked}:{})});
    const control=(id,label,menu,b)=>cmd(id,label,menu,()=>b.click(),b.disabled?'Unavailable for the current selection or index.':'');
    const node=mapData.nodes.find(n=>n.id===selected),mapView=view==='map';
    const result=[cmd('choose','Open folder…','File',openFolder),
      cmd('map','Graph canvas','View',()=>switchSurface('canvas'),'',mapView&&surfaceView==='canvas'),
      cmd('project-overview','Checkout overview','View',()=>switchSurface('project'),!activeProjectAuthority()?'Wait for checkout structure.':'',mapView&&surfaceView==='project'),
      cmd('table','Graph table','View',()=>switchSurface('table'),'',mapView&&surfaceView==='table'),
      ...['map','web','gravity'].map(mode=>cmd('presentation-'+mode,mode[0].toUpperCase()+mode.slice(1),'View',()=>{
        presentationSelect.value=mode;
        presentationSelect.dispatchEvent(new Event('change'));
      },'',presentation===mode)),
      {...control('evidence','Search & evidence','View',evidenceButton),checked:!mapView},
      cmd('explorer','Explorer','View',()=>togglePanel('explorer'),!mapView?'Show the graph canvas first.':'',!explorer.hidden),
      cmd('inspector','Inspector','View',()=>togglePanel('inspector'),!mapView?'Show the graph canvas first.':'',!inspector.hidden),
      control('overview','Overview and useful depth','View',overview),
      cmd('fit','Fit visible graph','View',()=>{scene.fit();revealSelection();},!mapView?'Show the graph canvas first.':''),
      cmd('zoom-in','Zoom in','View',()=>scene.zoom(1.2),!mapView?'Show the graph canvas first.':''),
      cmd('zoom-out','Zoom out','View',()=>scene.zoom(1/1.2),!mapView?'Show the graph canvas first.':''),
      cmd('expand','Expand selected branch','Graph',()=>expandBranch(selected),!mapView||!node||!expandable(node)||expansionBusy||mapData.status!=='ready'?'Select a folder in the canvas or Explorer.':''),
      cmd('collapse','Hide selected branch children','Graph',()=>collapseBranch(selected),!mapView||!node||!expandable(node)||!childrenShown(selected)?'Select an expanded branch.':''),
      cmd('enter-territory','Enter selected territory','Graph',()=>{setSolo(selected);void expandBranch(selected);},!mapView||presentation==='map'||!['folder','repository'].includes(node?.kind)?'Select a territory in Web or Gravity.':''),
      cmd('solo-territory','Solo selected territory','Graph',()=>setSolo(selected),!mapView||presentation==='map'||!['folder','repository'].includes(node?.kind)?'Select a territory in Web or Gravity.':''),
      cmd('hide-territory','Hide selected territory','Graph',()=>hideTerritory(selected),!mapView||presentation==='map'||node?.kind!=='folder'?'Select a folder territory in Web or Gravity.':''),
      cmd('restore-territories','Restore all territories','Graph',restoreTerritories,!mapView||!soloRoot&&!hiddenTerritories.size?'All territories are shown.':''),
      cmd('deeper','Expand next level','Graph',()=>expandLevels(settings.expansionDepth),!mapView||expansionBusy||!anchor?'No current expandable graph.':''),
      cmd('more','Load more for selected branch','Graph',()=>loadBranchMore(),more.disabled?'This branch has no available continuation.':''),
      ...(!mapView?[]:[control('connections','Connections for selected item','Graph',neighbors)]),
      ...(!mapView?[]:[control('inspect','Inspect selected evidence','Graph',inspectEvidence)]),
      ...(!mapView?[]:[control('open','Open selected source','File',evidenceActions[0])]),
      ...(!mapView?[]:[control('history','Recorded changes','File',evidenceActions[3])]),
      ...(!mapView?[]:[control('add','Add selected evidence to chat','Graph',evidenceActions[1])]),
      ...(!mapView?[]:[control('ask','Ask about selected evidence','Graph',evidenceActions[2])]),
      cmd('export','Export current graph…','File',()=>showExportPreview(c),view!=='map'||!renderedProjection?'Show a current graph first.':''),
      control('recheck','Refresh now','Graph',recheck),
      ...(!reviewRemovals.hidden?[control('review-removals','Review source removals','Graph',reviewRemovals)]:[]),
      ...(!retry.hidden?[control('retry','Retry indexing','Graph',retry)]:[]),
      ...(!readmit.hidden?[control('readmit','Readmit erased index','Graph',readmit)]:[]),
      control('observe-stop','Stop observing','Graph',detach),
      cmd('auto-context','Automatic code context','Graph',()=>{preference.checked=!preference.checked;preference.dispatchEvent(new Event('change'));},preference.disabled?'Preference unavailable.':'',preference.checked),
      cmd('settings','Graph settings…','View',showSettings),
      cmd('reload','Retry map read','Graph',()=>retryMapRead(),!codeMapMayRead(index)?'No readable scope.':''),
      cmd('layout-pause',layoutPaused?'Resume layout':'Pause layout','Graph',()=>{
        layoutPaused=!layoutPaused;
        scene.setLayoutPaused?.(layoutPaused);
        if(layoutPaused){stopProgression({keep:true});projectionStart?.abort();}
        else startVisibleProjection();
        paint();
      },!mapView||presentation==='map'?'Choose Web or Gravity.':'',layoutPaused),
      cmd('settle','Reheat and settle layout','Graph',()=>scene.relayout?.(),!mapView||presentation==='map'?'Choose Web or Gravity.':''),
      cmd('diagnostics','Coverage and diagnostics','View',()=>{diagnosticPanel.hidden=!diagnosticPanel.hidden;if(!diagnosticPanel.hidden)renderDiagnostics();},'',!diagnosticPanel.hidden)];
    for(const direction of ['both','incoming','outgoing'])result.push(cmd('direction-'+direction,'Connections: '+direction,'Graph',()=>{connectionDirection=direction;if(selected)void connect(selected);},!mapView||!selected||mapData.authority?.variant!=='published'?'Select published code evidence.':'',connectionDirection===direction));
    for(const relation of MAP_RELATIONS)result.push(cmd('relation-'+relation,'Show '+relation+' relationships','View',()=>{relations.has(relation)?relations.delete(relation):relations.add(relation);updateScene();paint();},!mapView?'Show the graph canvas first.':'',relations.has(relation)));
    if(view==='evidence'&&legacy)result.push(...(legacy.menuCommands?.(JSON.parse(c.evidence))||[]).map(descriptor=>({...descriptor,current:()=>menuIsCurrent(c)&&legacy?.menuIsCurrent?.(JSON.parse(c.evidence))})));
    return result;
  }
  presentationSelect.value=presentation;applySettings();resize();paint();if(workspace)startIndex();
  if(options.settingsHost)showSettings(false);
  function dispose(){if(disposed)return;resetProjectOverview();closeTaskContributions();clearDocumentEvidence();clearRecordedActivity();clearEvidence();stopProgression();projectionStart?.abort();stopFollowing();closeObservedWorker();stopBoundaries();releaseCanvasMenu();releaseTableMenu();releaseExplorerMenu();disposed=true;activityModel.clear();expansionSerial++;indexSerial++;folderSerial++;actionSerial++;preferenceSerial++;release?.();release=null;actionAborter.abort();capabilityAborter.abort();aborter.abort();settingsDispose?.();refreshSerial++;staging?.dispose();model.dispose();legacy?.dispose();scene.dispose();resizeObserver.disconnect();observer.disconnect();themeObserver.disconnect();subscribers.clear();}
  return {applyQualifiedImpact,run:(operation,extra={})=>operation==='overview'?loadMap('overview'):evidence(operation,extra),selectWorkspace,isCurrent:current,isMounted:()=>!disposed&&element.isConnected,
    observeActivity,openFolder,publishedContext,openSettings:showSettings,snapshot:()=>view==='evidence'&&legacy?legacy.snapshot():model.snapshot(),camera:()=>scene.snapshot().camera,presentationStats:()=>scene.snapshot(),menuCapture,menuScopeCurrent,menuIsCurrent,menuCommands,setVisible,dispose};
}
