// Real production command closures; no layout, native/backend or browser claim.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {graphResource,graphDisabledReason,executeCodeGraphNavigation,CODE_GRAPH_LABEL} from '../../static/js/copal/resourceGraphCommands.js';
import {createFilesSelectionModel} from '../../static/js/filesSelectionModel.js';
import {registerMenuDismiss,dismissTopMenu,_openMenuCount} from '../../static/js/escMenuStack.js';
const read=path=>fs.readFileSync(new URL('../../static/js/'+path,import.meta.url),'utf8');
const files=read('files.js'),notes=read('copal/notesFeature.js');
function closure(source,start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);return source.slice(a,b);}
function instantiate(code,deps,result){return new Function('deps',`const {${Object.keys(deps).join(',')}}=deps;${code};return ${result};`)(deps);}
const file={provider:'host',kind:'file',resource_id:'file-id',resource_ref:'file-ref',name:'source.py',path:'/authorized/source.py',capabilities:['open','stat','rename']};
const directory={provider:'host',kind:'folder',resource_id:'parent-id',resourceRef:'parent-ref',capabilities:['children']};
function descriptors(entries,{pickerMode=false,workspaceRoot=false}={}){
 const effects=[],noop=()=>{},state={owner:'fixture',workspaces:workspaceRoot?[{workspace:{id:'workspace',revision:7}}]:[],mode:'list',sort:{},shell:{classList:{contains:()=>false}},navigation:{canGoBack:()=>false,canGoForward:()=>false}};
 const deps={state,options:{pickerMode},CODE_GRAPH_LABEL,graphDisabledReason,openCapturedCodeGraph:s=>effects.push(['graph',s]),isDirectory:e=>e?.kind==='folder',
   isTextualEntry:()=>true,isCommandCaptureCurrent:()=>true,entrySelectionKey:e=>e.resource_id,
   renameWorkspace:(_item,current)=>effects.push(['rename',current()]),archiveWorkspace:(_item,current)=>effects.push(['archive',current()]),useWorkspace:id=>effects.push(['use',id]),
   createFilesResource:noop,openManagedDirectory:noop,confirmPickerEntry:noop,openManagedEntry:noop,downloadEntry:noop,
   uiModule:{styledConfirm:noop},formatBytes:noop,formatModified:noop,customizeExplorer:noop,previewEntry:noop,entryPath:e=>e?.path,
   isPreviewable:()=>true,transferCapabilities:()=>({copy:true,move:true}),FILES_MAX_DRAG_ITEMS:64,
   validateFilesSelectionGuard:()=>({scope:{parentRef:'parent'}}),setFilesClipboard:noop,filesClipboard:null,pasteFilesClipboard:noop,
   ensureSelectionModel:noop,activeColumn:()=>directory,applyModelSelection:noop,
   entryActionChoices:()=>[{action:'rename',label:'Rename'}],commonActionChoices:()=>[],performSelectedBulkAction:noop,performManagedAction:noop,
   isCanvasImageEntry:()=>false,openEntryInCanvas:noop,FILE_SORT_OPTIONS:[],applyPrimarySort:noop,toggleNavigation:noop,api:{refresh:noop},
   managedUp:noop,openProvider:noop,CLANKER_HOME_PROVIDER:'host',toggleCurrentFavorite:noop,newWindow:noop,split:noop,focusOtherPane:noop,closeSplit:noop,id:'owned',
   revealResource:noop,navigator:{clipboard:{writeText:noop}},window:{dispatchEvent:noop},CustomEvent:class{},
 };
 const snapshot={entries:workspaceRoot?[{...file,kind:'folder',workspaceRootId:'workspace',capabilities:['children']}]:entries,directory,selection:{},creation:null,navigating:false};
 const fn=instantiate(closure(files,'function commandDescriptors(','function activate('),deps,'commandDescriptors');
 return {items:fn(snapshot),snapshot,effects,state};
}
test('Files shared descriptor authority distinguishes file/folder/workspace/multi/background and picker',()=>{
 const one=descriptors([file]);assert.equal(one.items.find(x=>x.id==='files.code-graph').disabledReason,'');
 assert(!one.items.some(x=>x.id==='files.workspace-archive'));
 const multi=descriptors([file,{...file,resource_id:'other'}]);assert(multi.items.find(x=>x.id==='files.code-graph').disabledReason);
 assert(multi.items.find(x=>x.id==='files.action-rename')===undefined);
 const background=descriptors([]);assert.equal(background.items.find(x=>x.id==='files.code-graph').disabledReason,'');
 const root=descriptors([file],{workspaceRoot:true});assert.deepEqual(root.items.filter(x=>x.id.startsWith('files.workspace-')).map(x=>x.label),['Use as workspace','Rename workspace','Archive workspace','Open Workspace Hexes']);
 const picker=descriptors([file],{pickerMode:true});assert(picker.items.some(x=>x.id==='files.code-graph'));assert(!picker.items.some(x=>x.id==='files.action-rename'||x.id==='files.workspace-archive'||x.id==='files.new-file'));
});
test('Files popup and context are descriptor consumers, no second workspace action array or stock duplicate',()=>{
 assert(files.includes("commandDescriptors(captured).filter(item => item.id.startsWith('files.workspace-')"));
 const adapter=closure(files,'function sharedFilesMenuAdapter()','async function openEntryInCanvas');
 assert(adapter.includes('ownsObjectCommands:true'));assert(adapter.includes('commandDescriptors(captured).find'));
 assert(adapter.includes('directoryOnly:!tree&&!entry'));
 const global=read('custom-context-menu.js');assert(global.includes('!registered?.adapter?.ownsObjectCommands'));assert(global.includes('!request.adapter?.ownsObjectCommands'));
});
test('Workspace rename and archive recheck captured owner after async prompt, before service mutation',async()=>{
 for(const [name,end] of [['renameWorkspace','async function archiveWorkspace'],['archiveWorkspace','function showTreeActions']]){
  let still=true,mutations=0;
  const deps={uiModule:{styledPrompt:async()=>{still=false;return 'New';},styledConfirm:async()=>{still=false;return true;}},
   filesFacadeClient:{updateWorkspace:async()=>mutations++},state:{currentWorkspaceId:'other'},loadWorkspaceCatalog:async()=>{},document:{dispatchEvent(){}},CustomEvent:class{},setStatus(){}};
  const fn=instantiate(closure(files,`async function ${name}`,end),deps,name);
  await fn({workspace:{id:'captured',name:'Old',revision:7}},()=>still);assert.equal(mutations,0);
 }
});
test('Editor navigation adapters preserve editing adapter capture/current/execute and shared resource command identity',async()=>{
 const registration=closure(notes,'  function registerEditorContextMenu(','  function attachmentCursor(');
 const selection={},editor={view:{dom:{isConnected:true}},focus(){}};const captured={editor};let current=true,run=0,editingRun=0,adapter;
 const editing={capture:()=>({ranges:[selection]}),commands:()=>[{id:'copy',label:'Copy'}],isCurrent:()=>current,execute:async()=>editingRun++};
 const deps={window:{openClankContextMenu:{registerAdapter:(_host,a)=>{adapter=a;return ()=>{};}}},state:{},
   createCodeMirrorContextAdapter:()=>editing,captureNotesWorkbench:()=>captured,notesWorkbenchCurrent:()=>current,
   notesWorkbenchCommands:()=>[{id:'editor.code-graph',label:CODE_GRAPH_LABEL,menu:'Go',disabledReason:'',run:async()=>run++}],insertTemplate(){},createFromTemplate(){}};
 instantiate(registration,deps,'registerEditorContextMenu')({editor});const request={adapterContext:adapter.capture()};
 assert.equal(request.adapterContext.ranges[0],selection);assert.deepEqual(adapter.commands(request).map(c=>c.id),['copy','editor.code-graph']);
 await adapter.execute('copy',request);await adapter.execute('editor.code-graph',request);assert.equal(editingRun,1);assert.equal(run,1);
 current=false;await assert.rejects(adapter.execute('editor.code-graph',request));assert.equal(run,1);
});
test('Saved Host graph resource comes from resource/save identity, not document filename or unsaved body',()=>{
 const deps={hostHistoryResourceForDocument:doc=>doc.resource?.key?.provider==='host'?{resourceId:doc.resource.key.resourceId,resourceRef:doc.resource.locator.opaqueRef}:null};
 const fn=instantiate(closure(notes,'  function graphResourceForDocument(','  async function openEditorCodeGraph('),deps,'graphResourceForDocument');
 const doc={name:'pretend.py',text:'UNSAVED PRIVATE',resource:{key:{provider:'host',resourceId:'exact'},locator:{opaqueRef:'ref'},capabilities:{read:true}}};
 assert.deepEqual(fn(doc),{provider:'host',kind:'file',resource_id:'exact',resource_ref:'ref',name:'pretend.py',capabilities:['open']});
 assert.equal(fn({...doc,resource:{key:{provider:'copal'}}}),null);assert(!JSON.stringify(fn(doc)).includes('UNSAVED'));
});
test('Files own binding generation advances without changing real captured selection scope; unrelated owner/selection still expires',()=>{
 const model=createFilesSelectionModel({scope:{owner:'fixture',workspace:'chat-unchanged',provider:'host',parentRef:'parent-ref',query:'',column:''}});
 model.setItems([file,{...file,resource_id:'other',resource_ref:'other-ref'}]);model.select(model.orderedKeys()[0]);
 const state={owner:'fixture',currentWorkspaceId:'chat-unchanged',navigationGeneration:4,activeColumnIndex:0,columns:[{...directory}],selectionModels:new Map([[model.snapshot().scopeKey,model]]),nativeWindow:{id:'Files-owned',visible:true},lifecycleGeneration:7,contentGeneration:8,contentController:null,treeSelection:null};
 const deps={state,listeners:{signal:{aborted:false}},api:{},copalWorkspace:()=>'',activeColumn:()=>state.columns[0],filesPolicyGeneration:()=>state.navigationGeneration,
  destinationResourceRef:c=>c?.resourceRef||c?.resource_ref||'',ensureSelectionModel:()=>model,creationDestinationForEntry:()=>null};
 const guards=closure(files,'function captureFilesSelectionGuard(','function updateLibraryCommandControls(');
 const captures=closure(files,'function captureCommands(','async function openCapturedCodeGraph(');
 const f=instantiate(guards+captures,deps,'({captureCommands,isCommandCaptureCurrent})');
 const captured=f.captureCommands(),scope=model.snapshot().scopeKey;
 assert.equal(f.isCommandCaptureCurrent(captured),true);
 state.navigationGeneration=5;
 assert.equal(f.isCommandCaptureCurrent(captured),false,'all old open menus expire');
 assert.equal(f.isCommandCaptureCurrent(captured,5),true,'only the validated binding can acknowledge its own new generation');
 assert.equal(model.snapshot().scopeKey,scope);assert.equal(captured.selection.filesGeneration,4);
 model.select(model.orderedKeys()[1]);assert.equal(f.isCommandCaptureCurrent(captured,5),false);
 state.owner='other';assert.equal(f.isCommandCaptureCurrent(captured,5),false);
});
function editorNavigation(){
 const state={contextEpoch:23},statuses=[],owner={window:{visible:true,setStatus:(message,error)=>statuses.push({message,error})}},calls=[],buffer={text:'UNSAVED',undo:['saved'],selection:{anchor:2,head:4}};
 let localCurrent=true,cancel=false,confirmations=0;
 const publicFile={provider:file.provider,kind:file.kind,name:file.name,capabilities:[...file.capabilities],id:file.resource_id,ref:file.resource_ref,policy_generation:4},parent={provider:'host',kind:'folder',id:'parent',ref:'parent',name:'Authorized parent',capabilities:['children'],policy_generation:4};
 const binding={generation:5,workspace:{id:'project',revision:1,archived:false},open_relative:'source.py',scope_kind:'parent_folder'};
 const client={stat:async()=>{calls.push('stat');return publicFile;},reveal:async()=>({resource:publicFile,parent}),codeGraphTarget:async()=>{calls.push('bind');return binding;},workspaceResource:async()=>({generation:5,workspace:binding.workspace,resource:{...publicFile,policy_generation:5}})};
 const deps={context:()=>owner,state,filesFacadeClient:client,graphResource,CODE_GRAPH_LABEL,
  styledConfirm:async()=>{confirmations++;return !cancel;},document:{dispatchEvent:()=>{calls.push('refresh');owner.noteGraphPolicyEpoch=(owner.noteGraphPolicyEpoch||0)+1;}},CustomEvent:class{},
  executeCodeGraphNavigation:options=>executeCodeGraphNavigation({...options,handoff:async(_binding,capture)=>{assert(capture.isCurrent());calls.push(['handoff',capture.dirty]);return true;}})};
 const fn=instantiate(closure(notes,'  async function openEditorCodeGraph(','  async function refreshHostDocumentAfterHistoryRestore('),deps,'openEditorCodeGraph');
 return {state,owner,buffer,calls,client,statuses,publicFile,get confirmations(){return confirmations;},set cancel(v){cancel=v;},set current(v){localCurrent=v;},run:()=>fn(file,{generation:23,current:g=>localCurrent&&g===state.contextEpoch,dirty:true})};
}
test('Editor uses actual policy from stat, preserves local epoch/dirty/undo/selection, and refreshes only after exact handoff',async()=>{
 const f=editorNavigation(),before=structuredClone(f.buffer);const r=await f.run();assert.equal(r.ok,true);assert.deepEqual(f.buffer,before);assert.deepEqual(f.state,{contextEpoch:23});assert.deepEqual(f.calls,['stat','bind',['handoff',true],'refresh']);
});
test('Editor cancel, late pane change and policy event during authorization never bind or save',async()=>{
 const f=editorNavigation();f.cancel=true;assert.equal((await f.run()).code,'cancelled');assert.deepEqual(f.calls,['stat']);
 const g=editorNavigation();g.client.stat=async()=>{g.current=false;return g.publicFile;};assert.equal((await g.run()).ok,false);assert(!g.calls.includes('bind'));
 const h=editorNavigation();h.client.reveal=async()=>{h.owner.noteGraphPolicyEpoch=1;return {};};assert.equal((await h.run()).code,'stale');assert.deepEqual(h.calls,['stat']);
 const i=editorNavigation();i.client.stat=async()=>{throw Error('PRIVATE PATH CONTENT');};const result=await i.run();assert.equal(result.ok,false);assert(!JSON.stringify(result).includes('PRIVATE'));
});
test('Editor direct stat refuses mismatched identity, malformed envelope and invalid generation without navigation or writes',async()=>{
 for(const alter of [r=>({...r,id:'different-resource'}),r=>({resource:r}),r=>({...r,policy_generation:null})]){
  const f=editorNavigation(),before=structuredClone(f.buffer);f.client.stat=async()=>alter(f.publicFile);
  assert.equal((await f.run()).ok,false);assert(!f.calls.includes('bind'));assert(!f.calls.some(c=>Array.isArray(c)&&c[0]==='handoff'));assert.deepEqual(f.buffer,before);assert(f.statuses.some(s=>s.error===true));
 }
 const f=editorNavigation();f.client.stat=async()=>{f.state.contextEpoch++;return f.publicFile;};assert.equal((await f.run()).ok,false);assert(!f.calls.includes('bind'));
});
test('same stable Host id becoming a folder cannot bypass captured file parent/dirty confirmation',async()=>{
 const f=editorNavigation(),before=structuredClone(f.buffer);let reveals=0;
 f.client.stat=async()=>({...f.publicFile,kind:'folder',capabilities:['children','stat']});f.client.reveal=async()=>{reveals++;throw Error('must not reveal changed kind');};
 assert.equal((await f.run()).ok,false);assert.equal(reveals,0);assert.equal(f.confirmations,0);assert.deepEqual(f.calls,[]);assert.deepEqual(f.buffer,before);assert(f.statuses.some(s=>s.error===true));
});
function editorFreshness(){
 let doc={id:'saved-host',name:'caller.py',head:'saved-head'},scope='same-account-workspace';const leaf={id:'leaf',docId:doc.id,mode:'source',view:'text',pinned:false},workspace={left:{folderWorkspaceRoot:{id:'anchor'}}},folder={id:'authorized-folder'},buffer={localRevision:1},editor={view:{dom:{isConnected:true},state:{doc:{},selection:{ranges:[{anchor:4,head:4}],mainIndex:0}}}},cache={editor},owner={window:{visible:true,root:{isConnected:true}},noteWorkspace:workspace,noteLeafViews:new Map([['leaf',cache]]),noteBuffers:new Map([[doc.id,buffer]]),noteResourceRequestEpoch:4};let current=owner;
 const state={filesGeneration:5},deps={context:()=>current,ensureWorkspace:()=>workspace,activeLeaf:()=>leaf,groupForLeaf:()=>({id:'group'}),activeDoc:()=>doc,workspaceDocuments:()=>[doc],currentScope:()=>scope,state,editorCreationFolder:()=>folder,getEditorStatus:()=>({})};
 const f=instantiate(closure(notes,'  function workbenchLocalRevision(','  function notesWorkbenchCommands('),deps,'({captureNotesWorkbench,notesWorkbenchCurrent})'),target=f.captureNotesWorkbench();
 return{...f,target,owner,editor,buffer,leaf,state,workspace,cache,set ownerContext(v){current=v;},set scope(v){scope=v;},set doc(v){doc=v;}};
}
test('actual Editor predicate: confirmation focus alone is current, background explorer epoch alone expires generic ticket',()=>{
 const f=editorFreshness();assert(f.notesWorkbenchCurrent(f.target));
 // styledConfirm focuses OK then restores previousFocus. No captured model
 // property changes merely because focus moved outside CodeMirror.
 let activeElement=f.editor.view.dom;const previousFocus=activeElement;activeElement={role:'confirmation'};activeElement=previousFocus;assert.equal(activeElement,f.editor.view.dom);assert(f.notesWorkbenchCurrent(f.target));
 f.owner.noteResourceRequestEpoch++;assert.equal(f.notesWorkbenchCurrent(f.target),false);assert.equal(f.notesWorkbenchCurrent(f.target,{allowFolderRefresh:true}),true);
});
test('folder-independent predicate still refuses owner, scope, document, edit, policy and selection changes',()=>{
 for(const change of [f=>f.ownerContext={},f=>f.scope='different-account',f=>f.leaf.docId='different-doc',f=>f.buffer.localRevision++,f=>f.state.filesGeneration++,f=>f.editor.view.state.doc={},f=>f.editor.view.state.selection.ranges[0].head++,f=>f.cache.editor={},f=>f.owner.window.visible=false,f=>f.workspace.left.folderWorkspaceRoot={id:'different-anchor'}]){
  const f=editorFreshness();change(f);if(f.workspace.left.folderWorkspaceRoot.id==='different-anchor')assert.equal(f.notesWorkbenchCurrent(f.target),false);else assert.equal(f.notesWorkbenchCurrent(f.target,{allowFolderRefresh:true}),false);
 }
});
test('actual saved Graph descriptor alone tolerates Explorer completion while preserving captured document/edit/selection/policy',async()=>{
 const bind=f=>{let action;const deps={doc:f.target.doc,target:f.target,dirty:true,CODE_GRAPH_LABEL,graphResourceForDocument:()=>file,graphDisabledReason,notesWorkbenchCurrent:f.notesWorkbenchCurrent,
  openEditorCodeGraph:(_resource,options)=>options,add:(id,_label,_menu,run)=>{assert.equal(id,'code-graph');action=run;}};
  instantiate(closure(notes,'    const graphResource=graphResourceForDocument(doc);',"    add('reveal-files'"),deps,'null');return action();};
 const f=editorFreshness(),options=bind(f);assert.equal(options.generation,5);assert.equal(options.dirty,true);assert(options.current(5));f.owner.noteResourceRequestEpoch++;assert(options.current(5));assert.equal(f.notesWorkbenchCurrent(f.target),false,'general menu ticket stays strict');
 for(const change of [f=>f.ownerContext={},f=>f.scope='different-account',f=>f.leaf.docId='other',f=>f.buffer.localRevision++,f=>f.state.filesGeneration++,f=>f.editor.view.state.doc={},f=>f.editor.view.state.selection.ranges[0].head++,f=>f.cache.editor={},f=>f.owner.window.visible=false]){const x=editorFreshness(),capture=bind(x);change(x);assert.equal(capture.current(5),false);}
});
test('Files transient menu owns Escape and releases stack on any teardown',()=>{
 const state={nativeWindow:{visible:true}},active={},document={activeElement:active};let removed=0,focused=0;
 const code=closure(files,'function closeManagedActionMenu()','function chooseHostApplication(');
 const f=instantiate(code,{state,document,registerMenuDismiss},'({closeManagedActionMenu,ownFilesActionMenu})');
 const menu={contains:n=>n===active,remove:()=>removed++},anchor={isConnected:true,focus:()=>focused++};
 const before=_openMenuCount();f.ownFilesActionMenu(menu,anchor);assert.equal(_openMenuCount(),before+1);
 assert(dismissTopMenu());assert.equal(removed,1);assert.equal(focused,1);assert.equal(state.actionMenu,null);assert.equal(_openMenuCount(),before);
 f.closeManagedActionMenu();assert.equal(_openMenuCount(),before);
});
test('physical row descendants are normalized to their captured file owner, not current background',()=>{
 const row={},child={closest:selector=>selector==='[data-copal-context-object="file"]'?row:null};let selected=false,options;
 const deps={contextEntryForNode:n=>n===row?file:null,hooks:{__openClankFilesContextCapture:n=>{assert.equal(n,row);selected=true;}},captureCommands:o=>{options=o;return {captured:true};}};
 const adapter=instantiate(closure(files,'function sharedFilesMenuAdapter()','async function openEntryInCanvas'),deps,'sharedFilesMenuAdapter')();
 assert.deepEqual(adapter.capture(child),{captured:true});assert.equal(selected,true);assert.equal(options.directoryOnly,false);
});
