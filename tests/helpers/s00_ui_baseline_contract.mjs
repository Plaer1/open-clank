import assert from 'node:assert/strict';

export const S00_LIMITS = Object.freeze({graphHeightFraction:0.6, minimumLabelPx:11,
  maximumOverlapFraction:0.1, inputP95Ms:100, frameP95Ms:33});

export function measurementClassification(value,schedule=null){
  assert(value===undefined||value==='diagnostic-contended'||value==='controlled-baseline-scheduled','Unknown S00 measurement class');
  if(value==='controlled-baseline-scheduled'){
    assert.equal(schedule?.schema,'openclank.s00-ui-schedule/v1');assert.equal(schedule.rootApproved,true);
    assert.equal(schedule.noOwnedCompilerOrNativeBenchmark,true);assert.equal(schedule.runName,'headed-ui-08');
    assert.equal(schedule.observationKind,'media-only');assert.match(schedule.launchSha256,/^[a-f0-9]{64}$/);
    assert(Number.isSafeInteger(schedule.notBeforeMs)&&schedule.notBeforeMs<=Date.now()&&Date.now()-schedule.notBeforeMs<=600000,'Root must admit the actual clear slot immediately before launch');
    assert(Number.isSafeInteger(schedule.expiresAtMs)&&Date.now()<schedule.expiresAtMs&&schedule.expiresAtMs<=Date.now()+3600000,'Fresh root scheduling admission required');
  }
  return {measurementClass:value||'unclassified-diagnostic',performanceQualified:false,latencyQualified:false,gpuQualified:false,
    schedulingAdmission:value==='controlled-baseline-scheduled'?'Explicit root scheduling attestation; not an automatic performance acceptance':null,
    timingCaveat:value==='diagnostic-contended'?'Known concurrent native diagnostic workload; timings/distributions are functional diagnostics, not a controlled baseline or speedup claim':value==='controlled-baseline-scheduled'?'Scheduled without owned compile/native benchmark overlap; DOM measurements still do not prove paint, GPU execution or accepted latency budgets':'No controlled measurement admission; timings are diagnostic only'};
}

export function validatePartialColdResume(raw,identity,golden){
  for(const [key,value]of Object.entries(identity))assert.equal(raw[key],value,'Partial cold identity mismatch');
  assert.equal(raw.status,'failed');assert.equal(raw.browserClosed,true);assert.equal(raw.measurementClass,'diagnostic-contended');
  assert.deepEqual(raw.groups,['exact prior normal setup reused without setup request','normal authenticated headed shell','exact prior synthetic files retained; no new policy grant','normal synthetic cold folder choice 1']);
  assert.equal(raw.failures.at(-1),'UI observation expired: authorized synthetic folder');
  assert.deepEqual(raw.coldTrials.map(t=>t.number),[1]);assert.equal(raw.coldTrials[0].verdict.passed,false);
  assert.equal(raw.measurements.length,1);const stage=raw.measurements[0];assert.equal(stage.scenario,'graph-choice');assert.equal(stage.repetition,1);
  const expected=proveNativeRelationships(golden,stage.graph.receipt,stage.nativeGoldenProof.proofs);assert.deepEqual(stage.nativeGoldenProof.expected,expected);
  const verdict=usefulView(stage.graph,expected);assert.deepEqual(raw.coldTrials[0].verdict,verdict);assert.deepEqual(raw.failures,[...verdict.failures.map(f=>'Cold 1: '+f),'UI observation expired: authorized synthetic folder']);
  const prefix='/api/file-policy/workspaces/'+expected.receipt.workspace_id+'/index/';
  assert.equal(raw.nativeRequests.filter(r=>r.route===prefix+'open').length,1);
  assert(raw.nativeRequests.every(r=>r.status===200&&[prefix+'open',prefix+'status','/api/memory/code/graph'].includes(r.route)),'Earlier attempt started another scope or request failed');
  return {nextColdTrial:2,completedColdTrials:[1],earlierHead:expected.receipt};
}
export const asyncBrowserStatements=body=>`(async()=>{${body}\n})()`;
export function validateCompletedColdResume(raw,earlier,identity,golden){
  validatePartialColdResume(earlier,identity,golden);
  for(const [key,value]of Object.entries(identity))assert.equal(raw[key],value,'Completed cold identity mismatch');
  assert.equal(raw.status,'failed');assert.equal(raw.browserClosed,true);assert.equal(raw.measurementClass,'diagnostic-contended');assert.equal(raw.derivedCorporaSha256,earlier.derivedCorporaSha256);assert.deepEqual(raw.fixture,earlier.fixture);
  assert.deepEqual(raw.groups,['exact prior normal setup reused without setup request','normal authenticated headed shell','exact prior synthetic files retained; no new policy grant',... [2,3,4,5].map(n=>'normal synthetic cold folder choice '+n)]);
  assert.deepEqual(raw.coldTrials.map(t=>t.number),[2,3,4,5]);assert.equal(raw.measurements.length,4);
  const failures=[],routes=new Set(['/api/memory/code/graph']);
  for(let n=2;n<=5;n++){
    const stage=raw.measurements[n-2];assert.equal(stage.scenario,'graph-choice');assert.equal(stage.repetition,n);
    const expected=proveNativeRelationships(golden,stage.graph.receipt,stage.nativeGoldenProof.proofs);assert.deepEqual(stage.nativeGoldenProof.expected,expected);
    const verdict=usefulView(stage.graph,expected);assert.equal(verdict.passed,false);assert.deepEqual(raw.coldTrials[n-2].verdict,verdict);failures.push(...verdict.failures.map(f=>'Cold '+n+': '+f));
    const prefix='/api/file-policy/workspaces/'+expected.receipt.workspace_id+'/index/';assert(!routes.has(prefix+'open'),'Cold workspace reused');routes.add(prefix+'open');routes.add(prefix+'status');routes.add(prefix+'release');assert.equal(raw.nativeRequests.filter(r=>r.route===prefix+'open').length,1);assert(raw.nativeRequests.filter(r=>r.route===prefix+'release').length<=1,'Repeated earlier release');
  }
  assert(raw.nativeRequests.every(r=>r.status===200&&routes.has(r.route)),'Unexpected earlier native request');
  assert.deepEqual(raw.failures,[...failures,'Browser evaluation failed at warm-reopen-0; private expression omitted']);
  assert.deepEqual(raw.priorPartialCold.completedColdTrials,[1]);assert.equal(raw.priorPartialCold.nextColdTrial,2);assert.deepEqual(raw.priorPartialCold.earlierHead,earlier.measurements[0].graph.receipt);
  return {nextColdTrial:6,completedColdTrials:[1,2,3,4,5],warmupNeedsAttachment:true};
}
export function validateCompletedWarmResume(raw,cold,first,identity,golden){
  validateCompletedColdResume(cold,first,identity,golden);
  for(const [key,value]of Object.entries(identity))assert.equal(raw[key],value,'Completed warm identity mismatch');
  assert.equal(raw.status,'failed');assert.equal(raw.browserClosed,true);assert.equal(raw.measurementClass,'diagnostic-contended');assert.equal(raw.derivedCorporaSha256,cold.derivedCorporaSha256);assert.deepEqual(raw.fixture,cold.fixture);
  assert.deepEqual(raw.groups,cold.groups.slice(0,3));assert.deepEqual(raw.coldTrials,[]);assert.equal(raw.measurements.length,6);assert.deepEqual(raw.priorPartialCold.completedColdTrials,[1,2,3,4,5]);assert.equal(raw.priorPartialCold.nextColdTrial,6);assert.equal(raw.excludedWarmSessionAttachment.normalReindexPossible,true);assert.equal(raw.excludedWarmSessionAttachment.coldCredit,false);
  const baseline=cold.measurements.at(-1).graph.receipt,failures=[];
  for(let n=0;n<=5;n++){const stage=raw.measurements[n];assert.equal(stage.scenario,'graph-warm-reopen');assert.equal(stage.repetition,n);assert.equal(stage.excludedWarmup,n===0);assert.equal(stage.readyObserved,true);assert.equal(stage.sessionAttachment,n===0);
    const expected=proveNativeRelationships(golden,stage.graph.receipt,stage.nativeGoldenProof.proofs);assert.deepEqual(stage.nativeGoldenProof.expected,expected);for(const key of Object.keys(baseline).filter(k=>k!=='head_generation'))assert.equal(expected.receipt[key],baseline[key],'Warm scope changed');const v=usefulView(stage.graph,expected);assert.equal(v.passed,false);failures.push(...v.failures.map(f=>'Warm '+n+': '+f));}
  assert.deepEqual(raw.failures,[...failures,'Only exact generated Host Location']);const prefix='/api/file-policy/workspaces/'+baseline.workspace_id+'/index/';
  assert.equal(raw.nativeRequests.filter(r=>r.route===prefix+'open').length,6);assert.equal(raw.nativeRequests.filter(r=>r.route===prefix+'release').length,5);assert(raw.nativeRequests.every(r=>r.status===200&&[prefix+'open',prefix+'status',prefix+'release','/api/memory/code/graph'].includes(r.route)));
  return {nextColdTrial:6,completedColdTrials:[1,2,3,4,5],completedWarmReopens:5,skipWarm:true};
}
export function syntheticFixtureAncestor(revealed,target,fixtureName){
  const check=value=>{if(!value)throw Error('Exact synthetic fixture ancestry unavailable');};
  check(target?.provider==='host'&&target.name==='trial-05'&&target.id&&target.ref&&revealed?.provider==='host'&&revealed.resource?.id===target.id&&revealed.resource?.name===target.name);
  const ancestors=revealed.ancestors;check(Array.isArray(ancestors)&&ancestors.length>=2);check(ancestors.at(-1).name==='cold-corpora');const root=ancestors.at(-2);check(root.name===fixtureName&&ancestors.filter(a=>a.name===fixtureName).length===1&&root.provider==='host'&&root.ref&&root.id&&root.id!==target.id&&root.capabilities?.includes('children'));
  return root;
}
export function verifySyntheticFavorite(response,anchor){
  const saved=response?.resource;
  if(!saved||saved.provider!=='host'||saved.id!==anchor.id||saved.name!==anchor.name||!saved.ref||!saved.capabilities?.includes('children')||!/^place-[0-9a-f]{32}$/.test(saved.place_id||''))throw Error('Synthetic Favorite target identity unavailable');
  return true;
}
export function syntheticPreviewRow(browser,expected){
  const directory=browser.getCurrentDirectory();
  const entries=(directory?.entries||[]).filter(e=>e.resource_id===expected.id&&e.name===expected.name&&e.provider==='host');
  const rows=[...browser.element.querySelectorAll('.files-entry')].filter(e=>e.dataset.fileName===expected.name);
  if(expected.provider!=='host'||directory?.provider!=='host'||entries.length!==1||rows.length!==1||!entries[0].resource_ref||rows[0].dataset.resourceRef!==entries[0].resource_ref)throw Error('Exact current synthetic preview row unavailable');
  return rows[0];
}

export function distribution(values) {
  const sorted = values.filter(Number.isFinite).slice().sort((a,b)=>a-b);
  if (!sorted.length) return {samples:0,median:null,p95:null,max:null};
  const at = p => sorted[Math.max(0, Math.ceil(sorted.length*p)-1)];
  return {samples:sorted.length,median:at(0.5),p95:at(0.95),max:sorted.at(-1)};
}

export const S00_SCENARIOS=Object.freeze(['graph-choice','graph-warm-reopen','files-480','files-640','large-preview','editor-shared-image','editor-code-image','editor-text-only','combined-two-files-editors-graph']);
export function stageDurations(times) {
  const diff=(end,start)=>Number.isFinite(times[end])&&Number.isFinite(times[start])&&times[end]>=times[start]?times[end]-times[start]:null;
  return {pickerOpenToConfirmationMs:diff('choice','pickerOpened'),choiceToProgressMs:diff('progress','choice'),choiceToScopeCommitMs:diff('scopeCommit','choice'),
    choiceToFirstDrawableMs:diff('drawable','choice'),choiceToStructuralMs:diff('structural','choice'),
    choiceToPublishedMs:diff('published','choice'),choiceToUsefulMs:diff('useful','choice'),
    scopeCommitToUsefulMs:diff('useful','scopeCommit'),armToFirstDrawableMs:diff('drawable','armed'),
    armToPublishedMs:diff('published','armed'),armToUsefulMs:diff('useful','armed')};
}
export function mediaStageDurations(times) {
  const elapsed=key=>Number.isFinite(times.navigation)&&Number.isFinite(times[key])&&times[key]>=times.navigation?times[key]-times.navigation:null;
  return {navigationToRowsLoadedMs:elapsed('rowsLoaded'),navigationToImageDecodedVisibleMs:elapsed('imageDecodedVisible'),navigationToEditableDocumentMs:elapsed('documentReady')};
}

// These are current native declaration IDs, resolved by exact name AND path,
// never a file-name hash or a label-only join. API proof runs outside UI timing.
export function proveNativeRelationships(golden,receipt,proofs){
  const same=r=>JSON.stringify(Object.keys(receipt).sort().map(k=>[k,receipt[k]]))===JSON.stringify(Object.keys(r||{}).sort().map(k=>[k,r[k]]));
  assert(receipt?.head_generation&&receipt.workspace_id,'Published graph receipt required');
  assert.equal(proofs.length,golden.relationships.length);
  const edges=golden.relationships.map((wanted,n)=>{
    const proof=proofs[n];
    for(const page of [proof.caller,proof.target,proof.neighbors]){assert.equal(page.schema,'open-clank.code-query/v1');assert.equal(page.error,null);assert(same(page.receipt),'Native golden receipt changed');assert(!page.next_cursor,'Golden metadata query incomplete');}
    const declaration=(page,name,file)=>{assert.equal(page.operation,'search');const hits=page.hits.filter(h=>h.kind==='declaration'&&h.label===name&&h.properties?.rel_path===file);assert.equal(hits.length,1,'Exact native declaration identity required');assert.match(hits[0].id,/^decl:[0-9a-f]{1,128}$/i);return hits[0].id;};
    const from=declaration(proof.caller,wanted.caller,wanted.caller_file),to=declaration(proof.target,wanted.target,wanted.target_file);
    assert.equal(proof.neighbors.operation,'neighbors');assert(proof.neighbors.edges.some(e=>e.from===from&&e.to===to&&e.resolution_state===wanted.state),'Required native relationship/state absent');
    return {from,to,state:wanted.state};
  });
  return {groups:golden.groups,edges,nativeProofRequired:false,receipt};
}
export function validateUsefulSnapshots(stage,expected){
  const same=r=>r&&Object.keys(expected.receipt).every(k=>r[k]===expected.receipt[k]);
  const sample=stage.graphCandidatesTruncated?null:(stage.graphCandidates||[]).find(s=>same(s.graph.receipt)&&usefulView(s.graph,expected).passed);
  return {time:sample?.time??null,classification:'same-head native identities applied to recorded UI samples; no API response is counted as draw',candidatesTruncated:!!stage.graphCandidatesTruncated};
}
export function browserMediaMilestones(t=window.__s00MediaTarget){
 if(!t)return{};
 // Three owned hit-test samples reject ordinary opaque overlays. An image
 // with pointer-events:none may hit its immediate wrapper, not an unrelated
 // covering sibling. This is sampled DOM exposure, NOT compositor/GPU proof.
 const visible=e=>{if(!e)return false;for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(p.hidden||s.display==='none'||s.visibility!=='visible'||Number(s.opacity)<=0)return false;}
  const b=e.getBoundingClientRect();let left=Math.max(0,b.x),right=Math.min(innerWidth,b.right),top=Math.max(0,b.y),bottom=Math.min(innerHeight,b.bottom);
  // Overflow clips the painted image/content before hit testing. Sample the
  // exposed intersection, not covered portions of a larger image rectangle.
  for(let p=e.parentElement;p;p=p.parentElement){const s=getComputedStyle(p);if(/^(hidden|clip|scroll|auto)$/.test(s.overflowX)||/^(hidden|clip|scroll|auto)$/.test(s.overflowY)){const r=p.getBoundingClientRect();if(/^(hidden|clip|scroll|auto)$/.test(s.overflowX)){left=Math.max(left,r.x);right=Math.min(right,r.right);}if(/^(hidden|clip|scroll|auto)$/.test(s.overflowY)){top=Math.max(top,r.y);bottom=Math.min(bottom,r.bottom);}}}
  if(b.width<=0||b.height<=0||right<=left||bottom<=top)return false;
  return [.25,.5,.75].every(f=>{const h=document.elementFromPoint(left+(right-left)*f,top+(bottom-top)*f);return !!h&&(h===e||e.contains?.(h)||(getComputedStyle(e).pointerEvents==='none'&&h===e.parentElement));});};
 const decodedVisible=image=>{if(!image||!image.complete||!image.naturalWidth||!visible(image))return false;if(t.decodeRequested!==image){t.decodeRequested=image;t.decodedImage=null;image.decode().then(()=>{if(t.decodeRequested===image)t.decodedImage=image;}).catch(()=>{});}return t.decodedImage===image;};
 if(t.kind==='files'){const d=t.browser.getCurrentDirectory(),s=t.browser.captureCommands();if(d?.resourceId!==t.resourceId)return{};return{rowsLoaded:Array.isArray(d.entries)&&d.entries.length===t.count&&!d.nextCursor&&!s.navigating,imageDecodedVisible:decodedVisible([...t.browser.element.querySelectorAll('.files-thumbnail')].find(visible))};}
 if(t.kind==='preview'){const root=t.browser.element,heading=root.querySelector('.files-preview-head strong'),image=root.querySelector('.files-preview-image');return{imageDecodedVisible:heading?.textContent===t.name&&image?.naturalWidth===2048&&image?.naturalHeight===2048&&decodedVisible(image)};}
 if(t.kind==='editor'){const title=[...document.querySelectorAll('.copal-inline-title')].find(e=>e.textContent.trim()===t.name&&visible(e));const content=[...document.querySelectorAll('.cm-content')].find(e=>e.textContent.includes(t.cue)&&e.getAttribute('contenteditable')==='true'&&visible(e)&&!e.closest('[aria-busy="true"],[data-syntax-ready="loading"]'));const editor=content?.closest('.cm-editor');return{documentReady:!!title&&!!content,imageDecodedVisible:t.image&&!!title&&decodedVisible(editor?.querySelector('img'))};}
 return{};
}

// Explicit arranged-state evidence, not window.visible/residency. Every
// sampled point must belong to its own applet, inside the browser viewport.
export function browserSurfaceExposure(roots,labels){
 return roots.map((root,i)=>{const e=root?.querySelector('.oc-applet-frame'),b=e?.getBoundingClientRect();if(!b)return{label:labels[i],exposed:false};
  let owned=0;const style=getComputedStyle(e);const shown=!root.closest('.hidden,[hidden],.modal-minimized')&&style.display!=='none'&&style.visibility==='visible'&&Number(style.opacity)>0&&b.width>0&&b.height>0;
  if(shown)for(const fx of [.2,.5,.8])for(const fy of [.2,.5,.8]){const x=b.x+b.width*fx,y=b.y+b.height*fy,h=document.elementFromPoint(x,y);if(x>=0&&x<innerWidth&&y>=0&&y<innerHeight&&h&&root.contains(h))owned++;}
  return{label:labels[i],ownedSamples:owned,totalSamples:9,exposed:owned===9,rect:{x:b.x,y:b.y,width:b.width,height:b.height}};
 });
}

// Install BEFORE the normal folder choice or media navigation. The producer
// observes real trusted events/DOM only; it never chooses a folder or indexes.
// Root can stop the observation with __s00Baseline.stop(), not the native job.
export function installBrowserRecorder(graphObserve,mediaObserve,judge,expected,scenario,readMediaMilestones=null) {
  if(window.__s00Baseline)throw Error('S00 recorder already installed');
  const abort=new AbortController(),times={armed:performance.now()},frames=[],inputs=[],media=[],graphCandidates=[];
  let graphCandidatesTruncated=false,graphCandidateBytes=0,lastCandidateKey=null;
  let stopped=false,frame,lastFrame=performance.now(),activeUntil=0,pendingInput=null,mutated=false;
  let previousScope=document.querySelector('[data-code-workspace]')?.textContent;
  let previousStatus=document.querySelector('[data-code-index-status]')?.textContent;
  const first=(key)=>{if(times[key]===undefined)times[key]=performance.now();};
  const listener=event=>{
    if(!event.isTrusted)return;
    const element=event.target instanceof Element?event.target:null;
    if(event.type==='click'&&element?.closest('.code-workbench button')?.textContent.trim()==='Choose code folder')first('pickerOpened');
    if(event.type==='click'&&times.pickerOpened!==undefined&&element?.closest('#workspace-use'))first('choice');
    if(element?.closest('[data-code-viewport],.files-browser-body,.cm-editor')){activeUntil=performance.now()+1000;if(pendingInput===null)pendingInput=performance.now();}
  };
  for(const name of ['click','pointerdown','wheel','keydown'])document.addEventListener(name,listener,{capture:true,passive:true,signal:abort.signal});
  const observer=new MutationObserver(()=>{mutated=true;});observer.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
  let lastMedia=0,lastGraph=0,sampleCount=0,sampleCostMs=0;
  const tick=now=>{
    if(stopped)return;
    if(now<activeUntil&&frames.length<10000)frames.push(now-lastFrame);lastFrame=now;
    // rAF's timestamp is the frame-start clock and may precede an event that
    // arrived later in that frame. Use callback-time performance.now for the
    // DOM mutation observation; retain rAF timestamps only for frame spacing.
    const callbackNow=performance.now();
    if(pendingInput!==null&&mutated){if(inputs.length<1000&&callbackNow>=pendingInput)inputs.push(callbackNow-pendingInput);pendingInput=null;}
    if(pendingInput!==null&&callbackNow-pendingInput>1000)pendingInput=null;
    mutated=false;
    const scope=document.querySelector('[data-code-workspace]')?.textContent;
    if(scope&&scope!==previousScope){if(times.choice!==undefined)first('scopeCommit');previousScope=scope;}
    const status=document.querySelector('[data-code-index-status]')?.textContent;
    if(status&&status!==previousStatus){if(times.choice!==undefined)first('progress');previousStatus=status;}
    if(now-lastGraph>=100){const started=performance.now();
      const eligible=scenario==='graph-warm-reopen'||scenario==='graph-choice'&&times.choice!==undefined&&times.scopeCommit!==undefined;
      const graph=eligible?graphObserve():null;
      if(graph?.visibleNodes>0){first('drawable');if(graph.groups.length)first('structural');
        if(/^(Code index ready|Ready with partial coverage)/.test(status||''))first('published');
        if(expected.nativeProofRequired){if(graph.receipt?.head_generation&&/^(Code index ready|Ready with partial coverage)/.test(status||'')&&judge(graph,{...expected,edges:[],nativeProofRequired:false}).passed){
          const key=JSON.stringify(graph),bytes=new TextEncoder().encode(key).byteLength;
          if(key!==lastCandidateKey){if(bytes<=32768&&graphCandidates.length<64&&graphCandidateBytes+bytes<=262144){graphCandidates.push({time:now,graph});graphCandidateBytes+=bytes;lastCandidateKey=key;}else graphCandidatesTruncated=true;}
        }}
        else if(judge(graph,expected).passed)first('useful');}
      if(times.navigation!==undefined&&typeof readMediaMilestones==='function'){
        const milestones=readMediaMilestones();for(const key of ['rowsLoaded','imageDecodedVisible','documentReady'])if(milestones[key]===true)first(key);
      }
      sampleCount++;sampleCostMs+=performance.now()-started;lastGraph=now;}
    if(now-lastMedia>=1000&&media.length<3600){const started=performance.now();media.push({elapsedMs:now-times.armed,...mediaObserve()});lastMedia=now;sampleCostMs+=performance.now()-started;}
    frame=requestAnimationFrame(tick);
  };
  const snapshot=()=>({scenario,times:{...times},graphCandidates:[...graphCandidates],graphCandidateBytes,graphCandidatesTruncated,activeFrameIntervalsMs:[...frames],inputToMutationAtFrameMs:[...inputs],media:[...media],
    stopped,sampleCount,sampleCostMs,graphSampleIntervalMs:100,mediaSampleIntervalMs:1000,observationOnly:true,nativeCancellation:false,activeInputDefinition:'trusted input to observed DOM mutation at rAF; not compositor paint latency'});
  window.__s00Baseline={snapshot,beginNavigation(){first('navigation');},stop(){if(!stopped){stopped=true;times.stopped=performance.now();cancelAnimationFrame(frame);observer.disconnect();abort.abort();}return snapshot();}};
  frame=requestAnimationFrame(tick);return {armed:true,scenario};
}

// Semantic expectations are independent of hit/list length. An inventory of five
// files is not a hierarchy or a dependency map, regardless of the list count.
export function usefulView(observed, expected, limits=S00_LIMITS) {
  const failures=[];
  if(expected.nativeProofRequired)failures.push('Current same-head native topology proof not yet established');
  const haveGroups=new Set(observed.groups || []);
  const haveEdges=new Set([...(observed.edges||[]),...(expected.receipt?[]:(observed.semanticEdges||[]))].map(e=>`${e.from}\0${e.to}`));
  for (const group of expected.groups) if (!haveGroups.has(group)) failures.push(`Missing visible hierarchy: ${group}`);
  for (const edge of expected.edges) if (!haveEdges.has(`${edge.from}\0${edge.to}`)) failures.push(`Missing known topology: ${edge.from} → ${edge.to}`);
  if (!(observed.graphHeightFraction>=limits.graphHeightFraction)) failures.push('Graph occupies less than 60% of visible applet height');
  if (!(observed.labels>0) || observed.minimumLabelPx<limits.minimumLabelPx) failures.push('Default labels are not readable');
  if (observed.clippedLabels>0) failures.push('Default labels extend outside the visible graph');
  if (observed.overlapFraction>limits.maximumOverlapFraction) failures.push('Default labels overlap');
  if (!observed.allExpectedVisible) failures.push('Known structure requires manual search/paging/pan rescue');
  return {passed:failures.length===0,failures};
}

// Serialized and executed in the browser. Semantic group/edge identities are
// returned for the authorized fixture; no source text, network URLs, auth data,
// opaque resource references or response bodies are returned by this stage.
export function browserGraphObservation() {
  const root=document.querySelector('[data-code-graph]') || document.querySelector('.code-workbench');
  const svg=root?.querySelector('[data-code-viewport]');
  const rect=e=>{const b=e?.getBoundingClientRect();return b?{x:b.x,y:b.y,width:b.width,height:b.height,right:b.right,bottom:b.bottom}:null;};
  const box=rect(svg), applet=rect(root?.closest('.copal-view') || root);
  const visible=e=>{const b=e.getBoundingClientRect(),s=getComputedStyle(e);return b.width>0&&b.height>0&&s.visibility==='visible'&&s.display!=='none';};
  const labels=[...(svg?.querySelectorAll('text') || [])].filter(visible);
  // Computed SVG font-size is in user units. Include the screen transform so
  // a 12-unit label at camera/viewBox scale .25 is measured as 3 CSS pixels.
  const fontPixels=e=>{const size=parseFloat(getComputedStyle(e).fontSize),m=e.getScreenCTM?.();if(!m)return 0;
    const sum=m.a*m.a+m.b*m.b+m.c*m.c+m.d*m.d,det=m.a*m.d-m.b*m.c;
    return size*Math.sqrt(Math.max(0,(sum-Math.sqrt(Math.max(0,sum*sum-4*det*det)))/2));};
  const bounds=labels.map(rect);
  let overlaps=0;
  for(let i=0;i<bounds.length;i++)for(let j=i+1;j<bounds.length;j++){
    const a=bounds[i],b=bounds[j];if(a.x<b.right&&a.right>b.x&&a.y<b.bottom&&a.bottom>b.y)overlaps++;
  }
  const left=Math.max(0,box?.x||0,applet?.x||0), top=Math.max(0,box?.y||0,applet?.y||0);
  const right=Math.min(innerWidth,box?.right||0,applet?.right||0), bottom=Math.min(innerHeight,box?.bottom||0,applet?.bottom||0);
  const ownedVisible=e=>{
    let opacity=1;for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);opacity*=Number(s.opacity??1);if(p.hidden||s.display==='none'||s.visibility!=='visible'||opacity<=0.01)return false;}
    const b=e.getBoundingClientRect();if((b.width<=0&&b.height<=0)||b.x<left||b.y<top||b.right>right||b.bottom>bottom)return false;
    const points=[];
    const painted=primitive=>{const s=getComputedStyle(primitive),tag=primitive.tagName?.toLowerCase();if(!tag||['g','svg'].includes(tag))return false;
      const colorAlpha=color=>{if(!color||color==='none'||color==='transparent')return 0;const alpha=String(color).match(/(?:,|\/)\s*([\d.]+)\s*\)$/);return alpha&&(/^(rgba|hsla)\(/.test(color)||color.includes('/'))?Number(alpha[1]):1;};
      const stroke=colorAlpha(s.stroke)*Number(s.strokeOpacity??1)>0.01&&parseFloat(s.strokeWidth)>0;
      const fill=colorAlpha(s.fill)*Number(s.fillOpacity??1)>0.01;
      return tag==='line'?stroke:stroke||fill;};
    // SVG line bounds can have zero height; sample actual stroked path points.
    if(typeof e.getPointAtLength==='function'&&e.getScreenCTM?.()){
      const length=e.getTotalLength(),m=e.getScreenCTM();for(const f of [.25,.5,.75]){const p=e.getPointAtLength(length*f);points.push({x:m.a*p.x+m.c*p.y+m.e,y:m.b*p.x+m.d*p.y+m.f,primitive:e});}
    }else{
      points.push({x:b.x+b.width/2,y:b.y+b.height/2,primitive:e});
      if(e.matches?.('[data-code-group],[data-code-point]'))for(const child of e.querySelectorAll('text,circle,rect,path,polygon')){const c=child.getBoundingClientRect();points.push({x:c.x+c.width/2,y:c.y+c.height/2,primitive:child});}
    }
    return points.some(p=>{if(p.x<left||p.y<top||p.x>=right||p.y>=bottom)return false;const hit=document.elementFromPoint(p.x,p.y);if(!hit)return false;
      if((hit===e&&painted(e))||(e.contains(hit)&&painted(hit)))return true;
      // Painted text/edges may intentionally opt out of hit testing. Permit
      // only the same owning SVG at that point, never an HTML/external overlay.
      const owner=p.primitive.ownerSVGElement;
      return painted(p.primitive)&&getComputedStyle(p.primitive).pointerEvents==='none'&&owner&&hit===owner;});
  };
  const groups=[...(svg?.querySelectorAll('[data-code-group]')||[])],edges=[...(svg?.querySelectorAll('[data-code-edge]')||[])],nodes=[...(svg?.querySelectorAll('[data-code-point]')||[])];
  const visibleGroups=groups.filter(ownedVisible),visibleEdges=edges.filter(ownedVisible),visibleNodes=nodes.filter(ownedVisible);
  // Existing consumer rows carry the complete projected label, unlike the
  // shortened SVG basename. This joins ONLY actually visible edges/endpoints;
  // it is not an additional backend query or inferred native relationship.
  const names=new Map([...(root?.querySelectorAll('[data-code-list] [data-code-id]')||[])].map(e=>[e.getAttribute('data-code-id'),e.textContent.split(' · ')[0]]));
  const clippedLabels=bounds.filter(b=>b.x<left||b.y<top||b.right>right||b.bottom>bottom).length;
  let receipt=null;try{const r=JSON.parse(root?.querySelector('[data-code-detail]')?.dataset.receipt||'null');if(r&&typeof r==='object')receipt=Object.fromEntries(['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'].map(k=>[k,r[k]]));}catch{}
  return {receipt,present:Boolean(svg),renderer:svg?'svg':root?.querySelector('canvas')?'canvas-unclassified':'none',
    graphHeightFraction:applet?.height?Math.max(0,bottom-top)/Math.min(innerHeight,applet.height):0,
    labels:labels.length,minimumLabelPx:labels.length?Math.min(...labels.map(fontPixels)):0,
    clippedLabels,overlapFraction:bounds.length?overlaps/bounds.length:0,
    viewport:{width:innerWidth,height:innerHeight},graphBox:box,visibleGraph:{left,top,right,bottom},
    renderedNodes:nodes.length,renderedEdges:edges.length,visibleNodes:visibleNodes.length,visibleEdges:visibleEdges.length,
    groups:visibleGroups.map(e=>e.getAttribute('data-code-group')),
    edges:visibleEdges.map(e=>({from:e.getAttribute('data-from'),to:e.getAttribute('data-to')})),
    semanticEdges:visibleEdges.map(e=>({from:names.get(e.getAttribute('data-from')),to:names.get(e.getAttribute('data-to'))})).filter(e=>e.from&&e.to),
    allExpectedVisible:clippedLabels===0&&labels.every(ownedVisible)&&visibleNodes.length===nodes.length&&visibleGroups.length===groups.length&&visibleEdges.length===edges.length};
}

// Reusable in the real normal-auth journey after opening only generated assets.
// Counts/pixels/visibility only: never return image URLs, selected text or buffers.
export function browserMediaObservation() {
  const visible=e=>{const b=e.getBoundingClientRect(),s=getComputedStyle(e);return b.width>0&&b.height>0&&b.bottom>0&&b.right>0&&b.top<innerHeight&&b.left<innerWidth&&s.display!=='none'&&s.visibility==='visible';};
  const images=root=>{const a=[...root.querySelectorAll('img')];return{mounted:a.length,visible:a.filter(visible).length,
    complete:a.filter(e=>e.complete&&e.naturalWidth>0).length,decodedBytes:a.reduce((n,e)=>n+e.naturalWidth*e.naturalHeight*4,0),largestPixels:Math.max(0,...a.map(e=>e.naturalWidth*e.naturalHeight))};};
  const files=[...document.querySelectorAll('.files-browser-body')].map(root=>({
    mountedRows:root.querySelectorAll('[data-files-selection-key]').length,
    visibleRows:[...root.querySelectorAll('[data-files-selection-key]')].filter(visible).length,
    thumbnails:images(root),scrollWidth:root.scrollWidth,clientWidth:root.clientWidth}));
  const editors=[...document.querySelectorAll('.cm-editor')].map(root=>({visible:visible(root),
    editable:Boolean(root.querySelector('[contenteditable="true"]')),images:images(root),
    syntaxReady:root.closest('[data-syntax-ready]')?.dataset.syntaxReady||null,
    ariaBusy:root.closest('[aria-busy]')?.getAttribute('aria-busy')||null}));
  return {files,editors,allVisibleImageDecodedBytes:[...document.querySelectorAll('img')].filter(visible).reduce((n,e)=>n+e.naturalWidth*e.naturalHeight*4,0),
    note:'DOM estimates, not actual decoder/GPU allocation; editors must be separately identified by normal journey route'};
}

export function validateCaptureConfig(config, approval, configSha256) {
  assert.equal(config.schema,'openclank.s00-ui-capture/v1');
  assert.equal(approval.schema,'openclank.s00-ui-capture-approval/v1');
  assert.equal(approval.config_sha256,configSha256);
  assert.equal(approval.disposable,true); assert.equal(config.disposable,true);
  assert.equal(approval.root,config.root); assert.equal(approval.origin,config.origin);
  assert(['source','package'].includes(config.mode));
  assert(['cold-data','warm-ready','warm-reopen'].includes(config.cacheClass));
  assert.equal(config.osCache,'not-flushed');
  assert(S00_SCENARIOS.includes(config.scenario));
  assert(Number.isSafeInteger(config.observationMs)&&config.observationMs>=5000&&config.observationMs<=5400000);
  assert.match(config.sourceManifestSha256,/^[a-f0-9]{64}$/);
  assert.match(config.syntheticUsername,/^s00-[a-z0-9-]{3,64}$/);
  const origin=new URL(config.origin), websocket=new URL(config.webSocketDebuggerUrl);
  assert.equal(origin.protocol,'http:'); assert.equal(origin.hostname,'127.0.0.1');
  assert(origin.port && origin.port!=='7777' && origin.pathname==='/');
  assert.equal(origin.username,''); assert.equal(origin.password,''); assert.equal(origin.search,''); assert.equal(origin.hash,'');
  assert.equal(websocket.protocol,'ws:'); assert.equal(websocket.hostname,'127.0.0.1');
  assert(websocket.port && websocket.port!=='7777'); assert.match(websocket.pathname,/^\/devtools\/page\/[A-Za-z0-9-]+$/);
  assert.equal(websocket.username,''); assert.equal(websocket.password,''); assert.equal(websocket.search,''); assert.equal(websocket.hash,'');
  assert(Number.isSafeInteger(config.server.pid)&&config.server.pid>1);
  assert(Number.isSafeInteger(config.browser.pid)&&config.browser.pid>1);
  assert.equal(typeof config.server.birth,'string'); assert(config.server.birth.length>10);
  assert.equal(typeof config.browser.birth,'string'); assert(config.browser.birth.length>10);
  assert.equal(config.server.pid,approval.server_pid); assert.equal(config.browser.pid,approval.browser_pid);
  assert(Number.isFinite(approval.expires_at_ms)&&Date.now()<approval.expires_at_ms);
  assert(approval.expires_at_ms-Date.now()<=15*60*1000);
  return config;
}
