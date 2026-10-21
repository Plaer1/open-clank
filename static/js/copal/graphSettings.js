// Browser-local presentation preferences. Native ceilings are not permissions.
const number=(label,value,min,max,step=1)=>({label,value,min,max,step});
const choice=(label,value,values)=>({label,value,values});
const flag=(label,value)=>({label,value});
export const GRAPH_SETTING_GROUPS={
  capacity:{
    maxFiles:number('Displayed code files',2000,1,2000),maxSymbols:number('Displayed code symbols',2000,1,2000),
    maxNodes:number('Displayed code nodes',2000,20,2000),maxEdges:number('Displayed code edges',5000,0,5000),
    retainedNodes:number('Retained code nodes',2000,200,2000),retainedEdges:number('Retained code edges',5000,800,5000),cacheMiB:number('Accounted cache (MiB)',4,1,16),
    documentItems:number('Displayed note / document items',300,10,2000),documentEdges:number('Displayed document edges',1000,0,5000),documentConnections:number('Document connections per item',100,1,1000),structureItems:number('Displayed structure items',2000,10,5000),structureDepth:number('Displayed structure depth',64,1,64),galaxyItems:number('Displayed Galaxy tracks / events',300,10,2000),galaxyEdges:number('Displayed Galaxy connections',1000,0,5000),connections:number('Connections per selected item',100,1,800),
    pageNodes:number('Nodes per read',200,10,200),pageEdges:number('Edges per read',800,10,800),pageGroups:number('Groups per read',50,1,50),pageKiB:number('Response budget (KiB)',256,1,256),
    initialDepth:number('Initial hierarchy levels',3,1,8),expansionDepth:number('Expand next levels',2,1,8),autoReads:number('Automatic read budget',16,1,64),autoNodes:number('Automatic loaded-node budget',1600,200,2000),
    explorerRows:number('Explorer rows per page',100,10,200),inspectorRows:number('Inspector relationship rows',50,5,200),evidenceMounted:number('Mounted evidence resources',200,10,200),evidenceLoaded:number('Retained evidence resources',1000,200,1000),evidenceMountedEdges:number('Mounted evidence edges',400,10,400),evidenceLoadedEdges:number('Retained evidence edges',1000,200,1000),evidencePaths:number('Retained static paths',100,1,100),evidenceFacets:number('Retained evidence facet buckets',1000,200,1000),evidenceWitnesses:number('Inspector witnesses',50,1,50),breadcrumbPaths:number('Breadcrumb paths',10,1,10),breadcrumbNodes:number('Breadcrumb nodes',80,1,80),
    searchRows:number('Evidence result rows',50,1,200),searchKiB:number('Evidence budget (KiB)',64,2,256),searchTokens:number('Evidence token budget',8192,2048,8192),searchHops:number('Evidence traversal hops',4,0,8),queryWork:number('Query work ceiling',4000000,1000,4000000,1000),
  },
  layout:{
    showFunctions:flag('Functions / methods',true),showClasses:flag('Classes / types / interfaces',true),showOtherSymbols:flag('Other declarations',true),nodeScope:choice('Code node scope','mixed',['mixed','files','symbols']),grouping:choice('Network grouping','folders',['free','folders']),groupDepth:number('Grouping levels',2,1,8),
    mapSpacing:number('Map spacing multiplier',1,.5,3,.1),nodeRadius:number('Node radius',6,3,18),nodeSizeBasis:choice('Node size basis','kind',['kind','connections']),clusterSpacing:number('Cluster spacing',300,100,1000,10),
    linkDistance:number('Link distance',150,40,500,5),linkStrength:number('Link strength',.06,.005,.3,.005),containmentStrength:number('Containment strength multiplier',.35,.05,1,.05),
    repulsion:number('Repulsion',1800,100,10000,100),gravity:number('Centering',.008,.001,.05,.001),damping:number('Velocity damping',.75,.2,.95,.05),collisionPadding:number('Collision padding',12,2,60),iterations:number('Finite force steps',180,20,400,10),
  },
  labels:{
    labels:choice('Labels','automatic',['off','selected','automatic','all']),labelDensity:number('Automatic label limit',160,10,500),labelSize:number('Label size',12,9,20),labelLength:number('Label characters',60,12,120),
    edgeWidth:number('Edge width',1.5,.5,4,.25),edgeOpacity:number('Edge opacity',.65,.1,1,.05),arrows:flag('Direction arrows',true),selectedEmphasis:flag('Emphasize selected relationships',true),
    showContainment:flag('Containment',true),showImport:flag('Imports',true),showCall:flag('Calls',true),showValueReference:flag('Value references',true),showTypeReference:flag('Type references',true),showInherits:flag('Inheritance',true),showImplements:flag('Implements',true),showRender:flag('Component rendering',true),showRoute:flag('Route registrations',true),showOverrides:flag('Method overrides',true),direction:choice('Relationship direction','both',['both','incoming','outgoing']),
  },
  motion:{
    motion:choice('Motion policy','system',['system','reduced','off']),transitionMs:number('Transition duration (ms)',350,0,1200,50),layoutMotion:flag('Interpolate layout',true),cameraMotion:flag('Animate camera',true),selectionMotion:flag('Selection emphasis',true),zoomSensitivity:number('Zoom sensitivity',1,.25,3,.25),preserveCamera:flag('Preserve view during updates',true),
  },
  rendering:{fps:choice('Requested animated FPS','auto',['auto','30','60','unlocked']),dpr:number('Pixel ratio ceiling',2,1,2,.25),background:flag('Background layer',true)},
  refresh:{autoRefresh:flag('Automatic source verification',true),verifySeconds:number('External-change verification (seconds)',60,15,600,5),debounceMs:number('Save/change debounce (ms)',1000,250,10000,250),cooldownSeconds:number('Verification cooldown (seconds)',15,5,300,5)},
  activity:{activity:flag('Successful source-open cues',false),activityMs:number('Source-open cue lifetime (ms)',800,200,2500,100),activityCount:number('Retained active cues',20,1,100)},
};
export const GRAPH_SETTING_FIELDS=Object.assign({},...Object.values(GRAPH_SETTING_GROUPS));
export const GRAPH_DEFAULTS=Object.fromEntries(Object.entries(GRAPH_SETTING_FIELDS).map(([k,v])=>[k,v.value]));
const valid=(key,value)=>{const f=GRAPH_SETTING_FIELDS[key];return !f?undefined:f.values?f.values.includes(value)?value:undefined:typeof f.value==='boolean'?typeof value==='boolean'?value:undefined:typeof value==='number'&&Number.isFinite(value)&&value>=f.min&&value<=f.max?Math.max(f.min,Math.min(f.max,Number((Math.round(value/f.step)*f.step).toFixed(6)))):undefined;};
function patch(input){return Object.fromEntries(Object.entries(input||{}).flatMap(([k,v])=>{const x=valid(k,v);return x===undefined?[]:[[k,x]];}));}
export function normalizeGraphSettings(input={}){input=input&&typeof input==='object'&&!Array.isArray(input)?input:{};return {version:1,presentation:['map','web','gravity'].includes(input.presentation)?input.presentation:'map',common:patch(input.common),modes:Object.fromEntries(['map','web','gravity'].map(mode=>[mode,patch(input.modes?.[mode])]))};}
export function resolveGraphSettings(input={},mode='map'){const s=normalizeGraphSettings(input);return {...GRAPH_DEFAULTS,...s.common,...(s.modes[mode]||{})};}
export function mountGraphSettings(root,{read,write,mode='map',family='code',isCurrent=()=>true,onChange=()=>{}}){
  const aborter=new AbortController(),signal=aborter.signal;
  const heading=document.createElement('h3');heading.textContent='Graph settings';
  const info=document.createElement('p');info.textContent='Saved in this browser for this account and workspace. Code limits are bounded by the current engine; accounted cache bytes are not total process memory. File and symbol limits are subordinate to the total-node and retained-data limits; they do not add together. Notes/documents have separate display limits in their Graph view. Index resources remain in System settings.';
  const scope=document.createElement('select');scope.setAttribute('aria-label','Graph settings scope');
  for(const [value,label]of [['common','Common defaults'],['map','Map override'],['web','Web override'],['gravity','Gravity override']]){const option=document.createElement('option');option.value=value;option.textContent=label;scope.append(option);}
  scope.hidden=family!=='code';scope.value=['map','web','gravity'].includes(mode)?mode:'common';
  const controls=document.createElement('div'),status=document.createElement('p');status.setAttribute('role','status');
  const save=(key,value)=>{if(!isCurrent())return;const s=normalizeGraphSettings(read());const target=scope.value==='common'?s.common:s.modes[scope.value];target[key]=value;write(s);onChange();status.textContent='Saved locally. Effective code capacity remains within current native ceilings.';};
  function paint(){controls.replaceChildren();const stored=normalizeGraphSettings(read()),values=resolveGraphSettings(stored,scope.value==='common'?'none':scope.value);
    for(const [group,allFields] of Object.entries(GRAPH_SETTING_GROUPS)){const familyFields=family==='documents'?['documentItems','documentEdges','documentConnections']:family==='structure'?['structureItems','structureDepth']:family==='galaxy'?['galaxyItems','galaxyEdges','documentConnections']:Object.keys(GRAPH_SETTING_FIELDS).filter(key=>!['documentItems','documentEdges','documentConnections','structureItems','structureDepth','galaxyItems','galaxyEdges'].includes(key));const fields=familyFields?Object.fromEntries(Object.entries(allFields).filter(([key])=>familyFields.includes(key))):allFields;if(!Object.keys(fields).length)continue;const section=document.createElement('details');section.open=group==='capacity';const title=document.createElement('summary');title.textContent=group[0].toUpperCase()+group.slice(1);section.append(title);
      for(const [key,f]of Object.entries(fields)){const label=document.createElement('label');label.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 0';label.append(document.createTextNode(f.label));const input=document.createElement(f.values?'select':'input');input.setAttribute('aria-label',f.label);
        if(f.values){for(const value of f.values){const o=document.createElement('option');o.value=value;o.textContent=value;input.append(o);}input.value=values[key];}
        else if(typeof f.value==='boolean'){input.type='checkbox';input.checked=values[key];}
        else{input.type='number';input.min=f.min;input.max=f.max;input.step=f.step;input.value=values[key];input.style.width='100px';}
        const networkOnly=['grouping','groupDepth','clusterSpacing','linkDistance','linkStrength','containmentStrength','repulsion','gravity','damping','collisionPadding','iterations'];if(scope.value==='map'&&networkOnly.includes(key)){input.disabled=true;label.title='Network/force control applies to Web or Gravity; common defaults remain editable.';label.append(document.createTextNode(' (Web / Gravity)'));}
        input.addEventListener('change',()=>{const value=f.values?input.value:typeof f.value==='boolean'?input.checked:Number(input.value),v=valid(key,value);if(v===undefined){status.textContent='Choose a value inside the displayed range.';return;}save(key,v);},{signal});label.append(input);section.append(label);}
      const reset=document.createElement('button');reset.type='button';reset.className='copal-btn';reset.textContent='Reset '+group;reset.addEventListener('click',()=>{if(!isCurrent())return;const s=normalizeGraphSettings(read()),target=scope.value==='common'?s.common:s.modes[scope.value];for(const key of Object.keys(fields))delete target[key];write(s);onChange();paint();},{signal});section.append(reset);controls.append(section);}
  }
  scope.addEventListener('change',paint,{signal});root.replaceChildren(heading,info,scope,controls,status);paint();return ()=>aborter.abort();
}
