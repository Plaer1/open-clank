import { graphBudgetSummary } from './graphBudgetSummary.js';
// Browser-local presentation preferences. Native ceilings are not permissions.
const number=(label,value,min,max,step=1)=>({label,value,min,max,step});
const choice=(label,value,values)=>({label,value,values});
const flag=(label,value)=>({label,value});
export const CODE_GRAPH_DISPLAY = Object.freeze({files:12000,nodes:16000,edges:24000,sceneBytes:12*1024*1024,cacheMiB:64});
export const GRAPH_SETTING_GROUPS={
  camera:{
    codeZoomMin:number('Minimum zoom (%)',.1,.1,99,.1),codeZoomMax:number('Maximum zoom (%)',800,100,1600,1),
    documentZoomMin:number('Minimum zoom (%)',1.25,1,99,.25),documentZoomMax:number('Maximum zoom (%)',5000,100,6500,1),
  },
  capacity:{
    maxFiles:number('Displayed code files',6000,1,CODE_GRAPH_DISPLAY.files),maxSymbols:number('Displayed code symbols',2000,1,CODE_GRAPH_DISPLAY.nodes),
    maxNodes:number('Displayed code nodes',8000,20,CODE_GRAPH_DISPLAY.nodes),maxEdges:number('Displayed code edges',5000,0,CODE_GRAPH_DISPLAY.edges),
    documentItems:number('Displayed note / document items',300,10,2000),documentEdges:number('Displayed document edges',1000,0,5000),documentConnections:number('Document connections per item',100,1,1000),structureItems:number('Displayed structure items',2000,10,5000),structureDepth:number('Displayed structure depth',64,1,64),galaxyItems:number('Displayed Galaxy tracks / events',300,10,2000),galaxyEdges:number('Displayed Galaxy connections',1000,0,5000),connections:number('Connections per selected item',100,1,800),
  },
  retention:{
    retainedNodes:number('Retained code nodes',8000,200,CODE_GRAPH_DISPLAY.nodes),retainedEdges:number('Retained code edges',5000,800,CODE_GRAPH_DISPLAY.edges),cacheMiB:number('Accounted cache (MiB)',8,1,CODE_GRAPH_DISPLAY.cacheMiB),
  },
  exploration:{
    pageNodes:number('Nodes per read',200,10,200),pageEdges:number('Edges per read',800,10,800),pageGroups:number('Groups per read',50,1,50),pageKiB:number('Response budget (KiB)',256,1,256),
    initialDepth:number('Initial hierarchy levels',3,1,8),expansionDepth:number('Expand next levels',2,1,8),autoReads:number('Automatic read budget',64,1,256),autoNodes:number('Automatic loaded-node budget',8000,200,CODE_GRAPH_DISPLAY.nodes),
    explorerRows:number('Explorer rows per page',100,10,200),inspectorRows:number('Inspector relationship rows',50,5,200),
  },
  evidence:{
    evidenceMounted:number('Mounted evidence resources',200,10,200),evidenceLoaded:number('Retained evidence resources',1000,200,1000),evidenceMountedEdges:number('Mounted evidence edges',400,10,400),evidenceLoadedEdges:number('Retained evidence edges',1000,200,1000),evidencePaths:number('Retained static paths',100,1,100),evidenceFacets:number('Retained evidence facet buckets',1000,200,1000),evidenceWitnesses:number('Inspector witnesses',50,1,50),breadcrumbPaths:number('Breadcrumb paths',10,1,10),breadcrumbNodes:number('Breadcrumb nodes',80,1,80),
    searchRows:number('Evidence result rows',50,1,200),searchKiB:number('Evidence budget (KiB)',64,2,256),searchTokens:number('Evidence token budget',8192,2048,8192),searchHops:number('Evidence traversal hops',8,0,8),queryWork:number('Query work ceiling',4000000,1000,4000000,1000),
  },
  layout:{
    showFunctions:flag('Functions / methods',true),showClasses:flag('Classes / types / interfaces',true),showOtherSymbols:flag('Other declarations',true),nodeScope:choice('Code node scope','mixed',['mixed','files','symbols']),grouping:choice('Network grouping','folders',['free','folders','workspaces']),groupDepth:number('Grouping levels',2,1,8),
    mapSpacing:number('Map spacing multiplier',1,.5,3,.1),nodeRadius:number('Node radius',6,3,18),nodeSizeBasis:choice('Node size basis','kind',['kind','connections']),clusterSpacing:number('Cluster spacing',300,100,1000,10),
    linkDistance:number('Link distance',150,40,500,5),linkStrength:number('Link strength',.06,.005,.3,.005),containmentStrength:number('Containment strength multiplier',.35,.05,1,.05),
    repulsion:number('Repulsion',1800,100,10000,100),gravity:number('Centering',.008,.001,.05,.001),damping:number('Velocity damping',.75,.2,.95,.05),collisionPadding:number('Collision padding',12,2,60),iterations:number('Finite force steps',180,20,400,10),
  },
  labels:{
    densityBins:flag('Group dense items when zoomed out',true),densityZoom:number('Density grouping zoom (%)',55,10,100,1),
    labels:choice('Labels','automatic',['off','selected','automatic','all']),labelDensity:number('Automatic label limit',160,10,500),labelSize:number('Label size',12,9,20),labelLength:number('Label characters',60,12,120),
    edgeWidth:number('Edge width',1.5,.5,4,.25),edgeOpacity:number('Edge opacity',.65,.1,1,.05),arrows:flag('Direction arrows',true),selectedEmphasis:flag('Emphasize selected relationships',true),
    showContainment:flag('Containment',true),showImport:flag('Imports',true),showCall:flag('Calls',true),showValueReference:flag('Value references',true),showTypeReference:flag('Type references',true),showInherits:flag('Inheritance',true),showImplements:flag('Implements',true),showRender:flag('Component rendering',true),showRoute:flag('Route registrations',true),showOverrides:flag('Method overrides',true),
    showManifestDependency:flag('Manifest dependencies',true),showSqlTableReference:flag('SQL table references',true),showCallbackRegistration:flag('Callback registrations',true),showEventPublish:flag('Event publishers',true),showEventConsume:flag('Event consumers',true),direction:choice('Relationship direction','both',['both','incoming','outgoing']),
  },
  motion:{
    motion:choice('Motion policy','system',['system','reduced','off']),transitionMs:number('Transition duration (ms)',350,0,1200,50),layoutMotion:flag('Interpolate layout',true),cameraMotion:flag('Animate camera',true),selectionMotion:flag('Selection emphasis',true),zoomSensitivity:number('Zoom sensitivity',1,.25,3,.25),
    preserveCamera:flag('Preserve view during updates',true),
  },
  rendering:{
    fps:choice('Default animated FPS','auto',['auto','30','60','unlocked']),
    dockedFps:choice('Docked animated FPS','inherit',['inherit','auto','30','60','unlocked']),
    expandedFps:choice('Expanded animated FPS','inherit',['inherit','auto','30','60','unlocked']),
    dpr:number('Pixel ratio ceiling',2,1,2,.25),background:flag('Background layer',true),
    fileNodeShape:choice('File node shape','circle',['circle','square','ring']),
    nodeGlow:flag('Selected node glow',false),nodeShimmer:flag('Animated node emphasis',true)
  },
  refresh:{autoRefresh:flag('Automatic source verification',true),verifySeconds:number('External-change verification (seconds)',60,15,600,5),debounceMs:number('Save/change debounce (ms)',1000,250,10000,250),cooldownSeconds:number('Verification cooldown (seconds)',15,5,300,5)},
  activity:{activity:flag('Successful source-open cues',false),agentActivity:flag('Observed agent-read cues (next turn)',false),activityTrail:flag('Followed worker trail',false),activityMs:number('Activity cue lifetime (ms)',800,200,2500,100),activityCount:number('Retained active cues',20,1,100)},
};
export const GRAPH_SETTING_FIELDS=Object.assign({},...Object.values(GRAPH_SETTING_GROUPS));
export const GRAPH_DEFAULTS=Object.fromEntries(Object.entries(GRAPH_SETTING_FIELDS).map(([k,v])=>[k,v.value]));
const valid=(key,value)=>{const f=GRAPH_SETTING_FIELDS[key];return !f?undefined:f.values?f.values.includes(value)?value:undefined:typeof f.value==='boolean'?typeof value==='boolean'?value:undefined:typeof value==='number'&&Number.isFinite(value)&&value>=f.min&&value<=f.max?Math.max(f.min,Math.min(f.max,Number((Math.round(value/f.step)*f.step).toFixed(6)))):undefined;};
function patch(input){return Object.fromEntries(Object.entries(input||{}).flatMap(([k,v])=>{const x=valid(k,v);return x===undefined?[]:[[k,x]];}));}
export function normalizeGraphSettings(input={}) {
  input=input&&typeof input==='object'&&!Array.isArray(input)?input:{};
  const choices=['map','web','gravity'];
  const saved=choices.includes(input.presentation);
  // Older envelopes persisted a value without recording how it was chosen.
  // Honor that compatibility value without claiming it was an explicit choice.
  const presentationPreference=saved && input.presentationPreference==='explicit' ? 'explicit'
    : saved && input.presentationPreference!=='default' ? 'legacy' : 'default';
  return {version:1,presentation:presentationPreference==='default'?'gravity':input.presentation,presentationPreference,physicsSets:normalizedPhysicsSets(input.physicsSets),
    common:patch(input.common),modes:Object.fromEntries(choices.map(mode=>[mode,patch(input.modes?.[mode])]))};
}
export function resolveGraphSettings(input={},mode='map'){const s=normalizeGraphSettings(input);return {...GRAPH_DEFAULTS,...s.common,...(s.modes[mode]||{})};}
const FAMILY_FIELDS = {
  documents: ['documentItems','documentEdges','documentConnections','documentZoomMin','documentZoomMax'],
  structure: ['structureItems','structureDepth'],
  galaxy: ['galaxyItems','galaxyEdges','documentConnections','documentZoomMin','documentZoomMax'],
};
const FAMILY_LABELS = {code:'Code', documents:'Journal & Wiki', structure:'Structure', galaxy:'Galaxy'};
const NOTE_FIELDS = new Set(Object.values(FAMILY_FIELDS).flat());
const NETWORK_FIELDS = new Set(['grouping','groupDepth','clusterSpacing','linkDistance','linkStrength','containmentStrength','repulsion','gravity','damping','collisionPadding','iterations']);
function normalizedPhysicsSets(input) {
  const result=[], names=new Set();
  for(const item of Array.isArray(input)?input.slice(0,12):[]){
    const name=typeof item?.name==='string'?item.name.trim():'';
    if(!name||name.length>60||names.has(name.toLowerCase()))continue;
    const values=Object.fromEntries(Object.entries(patch(item.values)).filter(([key])=>NETWORK_FIELDS.has(key)));
    if(Object.keys(values).length){names.add(name.toLowerCase());result.push({name,values});}
  }
  return result;
}
const LARGE_PROJECT_DETAIL = Object.freeze({
  maxFiles:CODE_GRAPH_DISPLAY.files,maxSymbols:CODE_GRAPH_DISPLAY.nodes,maxNodes:CODE_GRAPH_DISPLAY.nodes,
  maxEdges:CODE_GRAPH_DISPLAY.edges,retainedNodes:CODE_GRAPH_DISPLAY.nodes,retainedEdges:CODE_GRAPH_DISPLAY.edges,
  cacheMiB:CODE_GRAPH_DISPLAY.cacheMiB,autoNodes:CODE_GRAPH_DISPLAY.nodes,autoReads:256,
});
const SECTION_LABELS = {
  camera:'Zoom', capacity:'Display capacity', retention:'Map cache', exploration:'Paging & expansion', evidence:'Search & evidence',
  layout:'Nodes & layout', labels:'Labels & relationships',
  motion:'Motion & camera', rendering:'Rendering', refresh:'Index refresh', activity:'Activity cues',
};

export function mountGraphSettings(root, {read, write, mode='map', family='code', isCurrent=()=>true, onChange=()=>{}}) {
  const aborter = new AbortController();
  const settingsId = `graph-settings-${crypto.randomUUID()}`;
  const formatter = new Intl.NumberFormat(undefined, {maximumFractionDigits:6});
  let painted;
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (label, onClick, signal) => {
    const node = element('button', label, 'copal-btn');
    node.type = 'button';
    node.addEventListener('click', onClick, {signal});
    return node;
  };
  const panel = element('div', undefined, 'graph-settings');
  const heading = element('h3', `Graph settings · ${FAMILY_LABELS[family] || family}`);
  heading.tabIndex = -1;
  const header = element('div', undefined, 'graph-settings-header');
  header.append(heading);
  const scope = element('select');
  scope.setAttribute('aria-label', 'Graph settings scope');
  for (const [value,label] of [['common','Common defaults'],['map','Map override'],['web','Web override'],['gravity','Gravity override']]) {
    const option = element('option', label);
    option.value = value;
    scope.append(option);
  }
  scope.value = ['map','web','gravity'].includes(mode) ? mode : 'common';
  const scopeLabel = element('label', 'Apply to ', 'graph-settings-scope');
  scopeLabel.append(scope);
  const controls = element('div', undefined, 'graph-settings-groups');
  const status = element('p', undefined, 'graph-settings-status');
  status.setAttribute('role','status');
  const budget = element('details', undefined, 'graph-settings-budget');
  budget.append(element('summary', 'Effective capacity'));
  const budgetLines = element('div');
  budget.append(budgetLines);
  const targetFor = stored => scope.value === 'common' ? stored.common : stored.modes[scope.value];
  const resolve = () => resolveGraphSettings(read(), scope.value);
  function save(edit) {
    if (!isCurrent()) return false;
    const stored = normalizeGraphSettings(read());
    edit(targetFor(stored),stored);
    write(stored);
    status.textContent = 'Graph settings saved.';
    onChange();
    return true;
  }

  function refreshBudget(values) {
    budgetLines.replaceChildren(...Object.entries(graphBudgetSummary(values)).filter(([key])=>key!=='coverage').map(([,line])=>element('p', line)),
      element('p','Semantic code search limits and provider permission appear in Search & evidence.'));
  }

  function mountNumber(row, label, key, field, initial, signal) {
    const inputs = element('div', undefined, 'graph-setting-inputs');
    const slider = element('input', undefined, 'graph-setting-range');
    const exact = element('input', undefined, 'graph-setting-number');
    const bounds = element('div', undefined, 'graph-setting-bounds');
    bounds.id = `${settingsId}-bounds-${key}`;
    bounds.append(element('span', formatter.format(field.min)),element('span',formatter.format(field.max)));
    slider.type = 'range';
    slider.id = `${settingsId}-${key}`;
    slider.setAttribute('aria-label',field.label);
    label.htmlFor = slider.id;
    exact.type = 'number';
    exact.setAttribute('aria-label', `${field.label} exact value`);
    for (const input of [slider,exact]) {
      input.min = field.min;
      input.max = field.max;
      input.step = field.step;
      input.value = initial;
      input.setAttribute('aria-describedby', bounds.id);
    }
    exact.inputMode = field.step % 1 === 0 ? 'numeric' : 'decimal';
    function commit(input) {
      const value = valid(key,input.valueAsNumber);
      const before = resolve()[key];
      if (value === undefined) {
        status.textContent = `Enter a value from ${formatter.format(field.min)} to ${formatter.format(field.max)}.`;
        slider.value = exact.value = before;
        return;
      }
      slider.value = exact.value = value;
      if (value!==before && save(target=>{target[key]=value;}) && family==='code') refreshBudget(resolve());
    }
    // A pointer drag updates the displayed draft. Persist on commit so an
    // expensive layout or read-budget change does not run for every pixel.
    slider.addEventListener('input',()=>{exact.value=slider.value;},{signal});
    slider.addEventListener('change',()=>commit(slider),{signal});
    exact.addEventListener('change',()=>commit(exact),{signal});
    inputs.append(slider,exact);
    row.append(label,inputs,bounds);
  }

  function mountChoice(row, label, key, field, initial, signal) {
    const input = element(field.values ? 'select' : 'input');
    input.id = `${settingsId}-${key}`;
    input.setAttribute('aria-label',field.label);
    label.htmlFor = input.id;
    if (field.values) {
      for (const value of field.values) {
        const option = element('option', value==='unlocked' ? 'Uncapped' : value[0].toUpperCase()+value.slice(1));
        option.value = value;
        input.append(option);
      }
      input.value = initial;
    } else {
      input.type = 'checkbox';
      input.checked = initial;
    }
    input.addEventListener('change',()=>{
      const value = field.values ? input.value : input.checked;
      save(target=>{target[key]=value;});
    },{signal});
    row.classList.add('graph-setting-choice');
    row.append(label,input);
  }

  function mountPhysicsSets(signal) {
    const section=element('details',undefined,'graph-settings-group');
    section.dataset.group='physics-sets';
    section.append(element('summary','Named physics settings'));
    const saved=normalizeGraphSettings(read()).physicsSets;
    const picker=element('select');
    picker.setAttribute('aria-label','Saved physics setting');
    picker.append(element('option','Choose a set'));
    picker.firstElementChild.value='';
    for(const item of saved){
      const option=element('option',item.name);option.value=item.name;picker.append(option);
    }
    const name=element('input');
    name.type='text';name.maxLength=60;name.placeholder='Name this set';name.setAttribute('aria-label','Physics set name');
    const apply=button('Apply',()=>{
      const set=saved.find(item=>item.name===picker.value);
      if(set&&save(target=>Object.assign(target,set.values)))paint();
    },signal);
    const remove=button('Remove',()=>{
      if(picker.value&&save((_target,stored)=>{stored.physicsSets=stored.physicsSets.filter(set=>set.name!==picker.value);}))paint();
    },signal);
    apply.disabled=remove.disabled=true;
    picker.addEventListener('change',()=>{apply.disabled=remove.disabled=!picker.value;},{signal});
    const saveSet=button('Save current set',()=>{
      const label=name.value.trim(), existing=saved.find(item=>item.name.toLowerCase()===label.toLowerCase());
      if(!label||label.length>60){status.textContent='Enter a name for the physics set.';return;}
      if(saved.length>=12&&!existing){status.textContent='Remove a set before saving another. This workspace can keep 12.';return;}
      const values=resolve();
      if(save((_target,stored)=>{
        stored.physicsSets=stored.physicsSets.filter(set=>set.name!==existing?.name);
        stored.physicsSets.push({name:label,values:Object.fromEntries([...NETWORK_FIELDS].map(key=>[key,values[key]]))});
      }))paint();
    },signal);
    section.append(picker,apply,remove,name,saveSet);
    return section;
  }

  function paint() {
    painted?.abort();
    painted = new AbortController();
    const {signal} = painted;
    const opened = new Set([...controls.querySelectorAll('details[open]')].map(section=>section.dataset.group));
    const firstPaint = !controls.childElementCount;
    const initialOpen = family==='code' ? [] : ['camera','capacity'];
    controls.replaceChildren();
    const values = resolve();
    if (family==='code') refreshBudget(values);
    for (const [group, allFields] of Object.entries(GRAPH_SETTING_GROUPS)) {
      const fields = Object.entries(allFields).filter(([key])=>family==='code'
        ? !NOTE_FIELDS.has(key) && !(scope.value==='map' && NETWORK_FIELDS.has(key))
        : FAMILY_FIELDS[family]?.includes(key));
      if (!fields.length) continue;
      const section = element('details', undefined, 'graph-settings-group');
      section.dataset.group = group;
      section.open = firstPaint ? initialOpen.includes(group) : opened.has(group);
      section.append(element('summary', SECTION_LABELS[group]));
      if(family==='code'&&group==='capacity'){
        const preset=button('Use large project detail',()=>{
          if(save(target=>Object.assign(target,LARGE_PROJECT_DETAIL))){
            paint();status.textContent='Display, cache, and automatic expansion raised for this scope.';
          }
        },signal);
        section.append(preset);
      }
      for (const [key,field] of fields) {
        const row = element('div', undefined, 'graph-setting-row');
        const label = element('label', field.label, 'graph-setting-label');
        if (typeof field.value==='number') mountNumber(row,label,key,field,values[key],signal);
        else mountChoice(row,label,key,field,values[key],signal);
        section.append(row);
      }
      section.append(button('Reset ' + SECTION_LABELS[group].toLowerCase(), ()=>{
        if (save(target=>{for (const [key] of fields) delete target[key];})) paint();
      },signal));
      controls.append(section);
    }
    if(family==='code'&&scope.value!=='map'){
      const presets=mountPhysicsSets(signal);
      presets.open=opened.has(presets.dataset.group);
      controls.append(presets);
    }
  }
  const indexSettings = button('Code index settings…', ()=>{
    if (isCurrent()) window.settingsModule?.open?.('system');
  }, aborter.signal);
  scope.addEventListener('change', paint, {signal:aborter.signal});
  if (family==='code') header.append(scopeLabel);
  panel.append(header,controls,...(family==='code' ? [budget,indexSettings] : []),status);
  root.replaceChildren(panel);
  paint();
  return ()=>{painted?.abort();aborter.abort();};
}
