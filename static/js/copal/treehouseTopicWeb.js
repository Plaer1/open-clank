// Global topic-web adapter. Account-owned course maps remain on the legacy path.
const SYSTEM = 'openclank:treehouse-core';
const mainQuests = value => arr(value).sort((a,b)=>['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'].indexOf(a.id)-['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'].indexOf(b.id));
const sourceQuery = `source=system&sourceId=${encodeURIComponent(SYSTEM)}`;
const arr = value => Array.isArray(value) ? value : typeof value === 'string' ? [value] : Object.values(value || {});
export function topicSourceKey(source, entityId = '') {
  if (source?.kind === 'system') return JSON.stringify(['system', source.sourceId, entityId || source.courseId || '']);
  return JSON.stringify(['account', source?.ownerAccountId, source?.workspaceId, entityId || source?.courseId || '']);
}
export function topicProgressLabel(progress = {}) {
  if (progress.status === 'unavailable' || progress.status === 'invalid') return 'Progress unavailable';
  if (Number.isFinite(progress.completed) && Number.isFinite(progress.total)) return `${progress.completed}/${progress.total} work items${progress.complete ? ' · Complete' : ''}`;
  return `${progress.requiredComplete || 0}/${progress.requiredTotal || 0} required Quests · ${progress.electiveComplete || 0}/${progress.electiveTotal || 0} elective Quests${Number.isFinite(progress.percent)?` · ${progress.complete?100:Math.min(99,progress.percent)}% of required work`:''}`;
}
export function topicDisplayContent(unit) {
  const lines=String(unit.content||'').split('\n');
  if(lines[0].replace(/^#\s+/, '').trim().toLocaleLowerCase() === String(unit.title||'').trim().toLocaleLowerCase() && /^#\s+/.test(lines[0]))lines.shift();
  return lines.join('\n').trimStart();
}
export function topicPracticalAnswer(response, status, confirmed, evidenceText) {
  return { response, outcome:{status, learnerConfirmed:status === 'success' && !!confirmed, evidenceText} };
}
export function createTreeHouseTopicWeb(getContext) {
  const roots = new Map(), subjects = new Map(), units = new Map(), progress = new Map();
  let retryTarget=null, selected = {rootId:null,courseId:null,unitId:null}, privateMode = false, epoch = 0, navigation = 0, loading = false, error = '', scope = '', revision = '', restored = false;
  const disposers = [], drafts = new Map(), dismissedPrevious = new Map(), scrolls = new Map();
  let mutationTail=Promise.resolve(), mutationVersion=0;
  const context = () => getContext();
  const availability = () => context().ui.snapshot?.globalAvailability;
  const unavailable = () => ['activation-required','incompatible'].includes(availability()?.status);
  const catalogue = () => unavailable()?null:context().ui.snapshot?.globalCatalogue;
  const scopeKey = () => {const {ui} = context();return JSON.stringify([ui.token,ui.snapshot?.accountId,ui.snapshot?.workspace,catalogue()?.sourceId,availability()?.status]);};
  const storageKey = () => `openclank:treehouse-global:${context().ui.snapshot?.accountId || ''}`;
  const disposeAssets = () => {for (const dispose of disposers.splice(0)) dispose();};
  function sync() {
    const nextScope = scopeKey(), nextRevision = catalogue()?.packageRevision || '';
    if (scope !== nextScope || revision !== nextRevision) {
      const accountChanged = scope && JSON.parse(scope)[1] !== JSON.parse(nextScope)[1];
      epoch++; navigation++; mutationTail=Promise.resolve(); roots.clear(); subjects.clear(); units.clear(); progress.clear(); disposeAssets();
      scope = nextScope; revision = nextRevision; loading = false; error = '';
      if(unavailable()){selected={rootId:null,courseId:null,unitId:null};restored=false;}
      if (accountChanged) { restored = false; selected = {rootId:null,courseId:null,unitId:null}; drafts.clear(); dismissedPrevious.clear(); scrolls.clear(); }
    }
    if (!restored && catalogue()) {
      restored = true;
      try { const saved = JSON.parse(globalThis.localStorage?.getItem(storageKey()) || 'null'); if(saved?.rootId && arr(catalogue().mainQuests).some(root => root.id === saved.rootId)) selected = saved; } catch { /* stale local preference */ }
    }
  }
  const capture = () => ({epoch,scope:scopeKey(),revision});
  const current = origin => origin.epoch === epoch && origin.scope === scopeKey() && origin.revision === catalogue()?.packageRevision;
  const persist = () => globalThis.localStorage?.setItem(storageKey(),JSON.stringify(selected));
  const rerender = () => context().renderLoaded();
  const saveScroll = () => {if(selected.courseId && selected.unitId)scrolls.set(`${selected.courseId}:${selected.unitId}`,context().ui.body?.scrollTop||0);};
  const restoreScroll = () => {if(context().ui.body)context().ui.body.scrollTop=scrolls.get(`${selected.courseId}:${selected.unitId}`)||0;};
  function validate(data) {
    if (catalogue()?.sourceId !== SYSTEM || data.sourceRef?.kind !== 'system' || data.sourceRef?.sourceId !== SYSTEM || data.packageRevision !== revision) throw new Error('Curriculum changed. Reload TreeHouse.');
    return data;
  }
  async function read(path) {
    const captured = epoch, capturedScope = scopeKey();
    const value = await context().api(path);
    if (captured !== epoch || capturedScope !== scopeKey()) return null;
    return validate(value);
  }
  function retainProgress(courseId, value) {
    if (!courseId || !value?.progress) return;
    const old = progress.get(courseId);
    if (!old || Number(value.progressRevision) >= Number(old.progressRevision)) progress.set(courseId,{progress:value.progress,progressRevision:value.progressRevision,resetGeneration:value.resetGeneration});
  }
  async function rootDetail(rootId, fresh = false) {
    sync(); if (!fresh && roots.has(rootId)) return roots.get(rootId);
    const readVersion=mutationVersion;
    const value = await read(`/treehouse/main-quests/${encodeURIComponent(rootId)}`);
    if (value) {if(value.mainQuest?.id!==rootId)throw new Error('Main Quest identity does not match.');if(readVersion!==mutationVersion)value.mainQuest=arr(catalogue()?.mainQuests).find(root=>root.id===rootId)||value.mainQuest;roots.set(rootId,value);for (const subject of arr(value.subjects)) retainProgress(subject.id,subject);}
    return value;
  }
  async function subjectDetail(courseId, fresh = false) {
    sync(); if (!fresh && subjects.has(courseId)) return subjects.get(courseId);
    const value = await read(`/treehouse/courses/${encodeURIComponent(courseId)}/summary?${sourceQuery}`);
    if(value) {if(value.subject?.id!==courseId || value.sourceRef.courseId!==courseId)throw new Error('Quest identity does not match.');subjects.set(courseId,value);retainProgress(courseId,value);}
    return value;
  }
  async function unitDetail(courseId, unitId, fresh = false) {
    sync(); const key=topicSourceKey({kind:'system',sourceId:SYSTEM,courseId},unitId);
    if (!fresh && units.has(key) && !units.get(key).stale) return units.get(key);
    if(fresh)units.delete(key);
    const value=await read(`/treehouse/courses/${encodeURIComponent(courseId)}/activities/${encodeURIComponent(unitId)}?${sourceQuery}`);
    if(value) {if(value.subjectId!==courseId || value.unit?.id!==unitId || value.sourceRef.courseId!==courseId)throw new Error('Lesson identity does not match.');units.set(key,value);retainProgress(courseId,value);}
    return value;
  }
  function command(type, payload = {}, form = null) {
    sync();const origin=form?.origin || capture();
    if(form && !current(origin)) return Promise.reject(new Error('This task changed. Reload the lesson and review your previous answer before saving.'));
    if(type.startsWith('submission.') && !payload.assignmentRequirementDigest) return Promise.reject(new Error('Reload this task before saving: its requirement identity is missing.'));
    const task=mutationTail.then(()=>current(origin)?dispatchCommand(type,payload,origin,form):null);
    mutationTail=task.catch(()=>{});return task;
  }
  async function dispatchCommand(type,payload,origin,form) {
    const {ui,api,setStatus} = context(), courseId=payload.courseId;
    const state=courseId ? await subjectDetail(courseId,true) : null;
    if(!current(origin) || (courseId && !state)) return null;
    if(type.startsWith('submission.')) {
      const requirement=state?.workRequirements?.[payload.assignmentId];
      if(!requirement || requirement.requirementDigest!==payload.assignmentRequirementDigest || (form && (state.resetGeneration!==form.resetGeneration || form.packageRevision!==revision || form.accountId!==ui.snapshot.accountId || form.sourceId!==SYSTEM))) {
        throw new Error('This task or reset scope changed. Reload the lesson and review your previous answer before saving.');
      }
    }
    if(type==='progress.reset' && courseId && form && (state.progressRevision!==form.progressRevision || state.resetGeneration!==form.resetGeneration)) {
      throw new Error('Progress changed since this reset was confirmed. Reload and confirm the current scope again.');
    }
    const sourceRef={kind:'system',sourceId:SYSTEM,...(courseId?{courseId}:{})};
    const response=await api('/treehouse/commands',{method:'POST',body:JSON.stringify({type,actorId:ui.actorId,commandId:`${type}:${crypto.randomUUID()}`,expectedRevision:courseId?(type==='progress.reset' && form?form.progressRevision:state.progressRevision):null,payload:{...payload,sourceRef,packageRevision:revision,...(courseId?{resetGeneration:form?form.resetGeneration:state.resetGeneration}:{})}})});
    if(!current(origin)) return null;
    validate(response);mutationVersion++;context().onGlobalMutation?.();
    retainProgress(courseId,response);
    for(const subject of arr(response.subjects)) retainProgress(subject.id,subject);
    if(response.globalCatalogue) ui.snapshot.globalCatalogue=response.globalCatalogue;
    for(const [rootId,value] of roots) {
      const updated=arr(catalogue()?.mainQuests).find(root=>root.id===rootId);
      if(updated) value.mainQuest=updated;
    }
    if(type === 'progress.reset') {
      const resetIds=new Set(courseId?[courseId]:arr(payload.resetScopes).map(item=>item.courseId));
      for(const [key,record] of drafts)if(resetIds.has(record.courseId))drafts.delete(key);
      for(const [key,record] of dismissedPrevious)if(resetIds.has(record.courseId))dismissedPrevious.delete(key);
    }
    if(type.startsWith('submission.') || type === 'progress.reset') {
      // Only browsed unit details are cached. A prerequisite mutation can affect
      // another subject, so drop this bounded cache and recompute availability.
      for(const [key,value] of units)units.set(key,{...value,stale:true});
    }
    setStatus(response.result?.completionSatisfied === false ? 'Attempt saved · practical work pending' : 'Learning progress saved');
    return response;
  }
  function openHome(asPrivate=false,{record=true}={}) {
    sync();context().beforeNavigation?.();navigation++;saveScroll();privateMode=asPrivate;selected={rootId:null,courseId:null,unitId:null};context().ui.selectedCourse=null;persist();loading=false;error='';rerender();if(context().ui.body)context().ui.body.scrollTop=0;if(record)context().onTopicNavigate?.();return true;
  }
  async function openRoot(rootId,fresh=false,{record=true,canApply=()=>true}={}) {
    sync();if(!catalogue()||!arr(catalogue().mainQuests).some(x=>x.id===rootId))return false;
    context().beforeNavigation?.();retryTarget={rootId};saveScroll();const origin=capture(),request=++navigation;loading=true;error='';rerender();
    try {
      const value=await rootDetail(rootId,fresh);if(!value||!current(origin)||request!==navigation||!canApply())return false;
      selected={rootId,courseId:null,unitId:null};privateMode=false;persist();loading=false;rerender();if(context().ui.body)context().ui.body.scrollTop=0;if(record)context().onTopicNavigate?.();return true;
    }catch(e){if(current(origin)&&request===navigation)error=e.message;return false;}
    finally{if(current(origin)&&request===navigation){loading=false;rerender();}}
  }
  async function openSubject(courseId,unitId=null,fresh=false,{record=true,canApply=()=>true}={}) {
    sync();if(!catalogue())return false;context().beforeNavigation?.();retryTarget={courseId,unitId};saveScroll();const origin=capture(),request=++navigation;loading=true;error='';rerender();
    try {
      const value=await subjectDetail(courseId,fresh);if(!value||!current(origin)||request!==navigation||!canApply())return false;
      const metadata=value.subject,currentProgress=progress.get(courseId)?.progress||value.progress;
      const target=unitId||currentProgress?.resumeActivityId||arr(value.units)[0]?.id;
      if(unitId&&!arr(value.units).some(x=>x.id===unitId))throw Error('Lesson is unavailable.');
      if(target){const detail=await unitDetail(courseId,target,fresh);if(!detail||!current(origin)||request!==navigation||!canApply())return false;await command('course.open',{courseId,activityId:target});}
      if(!current(origin)||request!==navigation||!canApply())return false;
      selected={rootId:metadata.mainQuestId,courseId,unitId:target};privateMode=false;persist();loading=false;rerender();restoreScroll();if(record)context().onTopicNavigate?.();return true;
    }catch(e){if(current(origin)&&request===navigation)error=e.message;return false;}
    finally{if(current(origin)&&request===navigation){loading=false;rerender();}}
  }
  async function restore(location,canApply=()=>true){
    sync();if(location.privateMode)return canApply()&&openHome(true,{record:false});
    if(!catalogue())return false;
    if(location.courseId)return openSubject(location.courseId,location.unitId,false,{record:false,canApply});
    if(location.rootId)return openRoot(location.rootId,false,{record:false,canApply});
    return canApply()&&openHome(false,{record:false});
  }
  async function reset(kind, id) {
    sync(); const origin=capture();
    const {styledConfirm}=context();
    let resetScopes=[],payload={},resetForm=null;
    if(kind==='subject') {const value=await subjectDetail(id,true);if(!value || !current(origin))return;payload={courseId:id};resetForm={origin,progressRevision:value.progressRevision,resetGeneration:value.resetGeneration};}
    else {
      const ids=kind==='root'?[id]:arr(catalogue().mainQuests).map(root=>root.id);
      for(const rootId of ids){const value=await rootDetail(rootId,true);if(!value || !current(origin))return;resetScopes.push(...arr(value.subjects).map(subject=>({courseId:subject.id,progressRevision:subject.progressRevision,resetGeneration:subject.resetGeneration})));}
      payload={...(kind==='root'?{mainQuestId:id}:{scope:'all-global'}),resetScopes};
    }
    const count=kind==='subject'?1:resetScopes.length;
    if(!await styledConfirm(`Reset your current work, submissions and resume in ${count} shared Quest${count===1?'':'s'}? Unsent work in this scope is discarded. Historical awards, private courses and unrelated drafts stay intact.`,{title:'Reset global learning progress',confirmText:'Reset progress',danger:true}))return;
    if(!current(origin))return;
    const result=await command('progress.reset',payload,resetForm);
    if(!result || !current(origin))return;
    if(selected.courseId)await unitDetail(selected.courseId,selected.unitId,true);
    rerender();
  }
  function action(fn) {
    return async event => {sync();const origin=capture(), button=event?.currentTarget; if(button)button.disabled=true;
      try {await fn();}catch(e){if(current(origin)){error=e.message;context().setStatus(e.message,true);rerender();}}
      finally {if(button?.isConnected)button.disabled=false;}
    };
  }
  function renderAvailability(root) {
    sync();if(!unavailable())return;
    const {h,ui,reloadTreeHouse,setStatus}=context(), incompatible=availability().status==='incompatible';
    const accountId=ui.snapshot.accountId, workspace=ui.snapshot.workspace;
    const sameAccount=()=>context().ui.snapshot?.accountId===accountId&&context().ui.snapshot?.workspace===workspace;
    const recheck=h('button',{type:'button',class:'copal-btn',text:'Recheck shared quests',onclick:async()=>{
      recheck.disabled=true;
      try{await reloadTreeHouse();}catch(error){if(sameAccount())setStatus(error.message,true);}
      finally{if(recheck.isConnected)recheck.disabled=false;}
    }});
    const help=h('button',{type:'button',class:'copal-btn',text:'Open upgrade help',onclick:async()=>{
      if(!sameAccount())return;
      if(typeof window.openClankHandbook==='function'){
        try{await window.openClankHandbook({presentation:'wiki'});}catch{if(sameAccount())setStatus('Open Settings → Help → Open Clank handbook, then TreeHouse.',true);}
      }else setStatus('Open Settings → Help → Open Clank handbook, then TreeHouse.',true);
    }});
    root.append(h('section',{class:'copal-card th-global-availability',role:'status','aria-label':'Shared quests availability'},
      h('h2',{text:incompatible?'Shared quests storage is incompatible':'Shared quests need activation'}),
      h('p',{text:incompatible?'An operator must inspect the learning database schema and use the supported upgrade or recovery procedure. Do not force activation over incompatible storage.':'Ask the operator to back up the learning database, stop all writers and use the schema-only offline activation procedure. Activation preserves courses and evidence; retirement is a separate operation.'}),
      h('p',{text:'Private courses remain available. Rechecking only reads availability; it does not upgrade the database. Open the handbook’s TreeHouse help and the repository upgrade guide for operator instructions.'}),
      recheck,help));
  }
  function render(root) {
    sync();disposeAssets();const {h,ui,renderMarkdown}=context();
    if(!catalogue() || catalogue().sourceId!==SYSTEM)return false;
    if(privateMode || ui.selectedCourse) {
      root.append(h('button',{class:'copal-btn',text:'Main Quests',onclick:()=>openHome()}));
      return false;
    }
    const button=(text,fn,attrs={})=>h('button',{class:'copal-btn',type:'button',text,onclick:action(fn),...attrs});
    const markdown=text=>h('div',{class:'th-lesson-body copal-meme-body'},renderMarkdown(text||'',new Set(),null,{treeHouseTopicLinks:true}));
    const progressBlock=p=>h('p',{class:'th-web-progress',role:'status',text:topicProgressLabel(p)});
    const heading=h('header',{class:'th-web-head'},h('h1',{text:'TreeHouse'}));
    root.append(heading);
    if(error)root.append(h('p',{role:'alert',text:error}),button('Retry',()=>/Curriculum changed/.test(error)?context().reloadTreeHouse():retryTarget?.courseId?openSubject(retryTarget.courseId,retryTarget.unitId,true):openRoot(retryTarget?.rootId||selected.rootId,true)));
    if(!selected.rootId) {
      root.append(h('p',{text:'Explore Quests and complete their required work.'}));
      const cards=h('div',{class:'th-web-root-grid'});
      for(const main of mainQuests(catalogue().mainQuests)) cards.append(h('article',{class:'th-web-card'},h('h2',{text:main.title}),progressBlock(main),button('Explore Quests',()=>openRoot(main.id),{'data-main-quest':main.id})));
      root.append(cards,button('Private courses',()=>openHome(true)),button('Reset all Main Quests',()=>reset('all')));return true;
    }
    const main=roots.get(selected.rootId), summary=main?.mainQuest || arr(catalogue().mainQuests).find(r=>r.id===selected.rootId);
    const crumbs=h('nav',{'aria-label':'Learning location',class:'th-web-breadcrumbs'},button('Main Quests',()=>openHome()),button(summary?.title||'Quests',()=>openRoot(selected.rootId)));
    root.append(crumbs);
    if(loading) {root.append(h('p',{role:'status',text:'Loading lesson…'}));return true;}
    if(!main && !selected.courseId) {if(!error){loading=true;void openRoot(selected.rootId,false,{record:false});}return true;}
    if(!selected.courseId) {
      root.append(h('h2',{text:summary?.title||'Quests'}),progressBlock(summary),h('p',{text:'Quests are independently available. Required Quests determine Main Quest completion.'}));
      const grid=h('div',{class:'th-course-grid'});
      for(const subject of arr(main.subjects)) {const p=progress.get(subject.id)?.progress||subject.progress;grid.append(h('article',{class:'th-web-card'},h('h3',{text:subject.title}),h('p',{text:subject.objective||''}),h('small',{text:subject.required?'Required Quest':'Elective Quest'}),progressBlock(p),button(p?.resumeActivityId?'Resume Quest':'Open Quest',()=>openSubject(subject.id),{'data-subject-id':subject.id})));}
      root.append(grid,button('Reset this Main Quest',()=>reset('root',selected.rootId)));return true;
    }
    const subject=subjects.get(selected.courseId), detail=units.get(topicSourceKey({kind:'system',sourceId:SYSTEM,courseId:selected.courseId},selected.unitId));
    if(!subject || !detail) {if(!error){loading=true;void openSubject(selected.courseId,selected.unitId,false,{record:false});}return true;}
    const p=progress.get(selected.courseId)?.progress||subject.progress;
    crumbs.append(h('span',{text:subject.subject.title}));root.append(progressBlock(summary),progressBlock(p));
    const layout=h('div',{class:'th-web-layout'}), outline=h('nav',{'aria-label':'Quest lessons',class:'th-web-outline'});
    for(const unit of arr(subject.units))outline.append(button(unit.title,()=>openSubject(selected.courseId,unit.id),{'aria-current':unit.id===selected.unitId?'page':false}));
    const lesson=h('article',{class:'th-lesson th-web-unit','data-treehouse-unit':detail.unit.id},h('h2',{text:detail.unit.title}),markdown(topicDisplayContent(detail.unit)));
    if(detail.contentSource && context().openLessonSource){const sourceOrigin=capture();lesson.append(button(detail.contentSource.canEditInFiles?'Edit source':'View source',()=>current(sourceOrigin)?context().openLessonSource(detail.subjectId,detail.unit.id,{sourceRef:detail.sourceRef,host:lesson}):null),button('Refresh lesson',async()=>{if(!current(sourceOrigin))return;await unitDetail(detail.subjectId,detail.unit.id,true);if(current(sourceOrigin))rerender();}));}
    for(const section of detail.unit.optionalSections||[])lesson.append(h('details',{class:'th-web-disclosure'},h('summary',{text:section.title}),markdown(section.content)));
    lesson.addEventListener('click',event=>{const link=event.target.closest?.('a[href^="#treehouse-unit="]');if(!link)return;event.preventDefault();try {const unitId=decodeURIComponent(link.getAttribute('href').slice('#treehouse-unit='.length));if(!/^(cs|games|ai):[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$/.test(unitId))throw new Error('Lesson link is unavailable.');void action(()=>openSubject(unitId.split(':').slice(0,2).join(':'),unitId))(event);}catch(e){context().setStatus(e.message,true);}});
    for(const assetId of detail.unit.assetIds||[]) {const host=h('section',{class:'th-web-asset','aria-label':'Interactive practice'});lesson.append(host);const origin=capture();void import('./treehouseLearningAssets.js').then(({mountLearningAsset})=>{if(!current(origin)||!host.isConnected)return;const mounted=mountLearningAsset({host,assetId,unitId:detail.unit.id,registry:detail.assetRegistry||[],theme:document.documentElement.dataset.theme});disposers.push(()=>mounted?.destroy?.());}).catch(()=>{if(host.isConnected)host.textContent='Interactive practice is unavailable. The written lesson remains available.';});}
    const publicSources=arr(detail.sources).filter(source=>{try {const u=new URL(source.url);return u.protocol==='https:' && !u.username && !u.password;}catch{return false;}});
    if(publicSources.length)lesson.append(h('details',{class:'th-web-disclosure'},h('summary',{text:'Sources'}),...publicSources.map(source=>h('p',{},h('a',{href:source.url,rel:'noopener noreferrer',target:'_blank',text:source.title||'Primary source'}),source.claim||source.supportedClaim? h('span',{text:` · ${source.claim||source.supportedClaim}`}):''))));
    const metadata=arr(subject.units).find(unit=>unit.id===detail.unit.id);
    const links=h('nav',{'aria-label':'Connected lessons',class:'th-web-links'});
    for(const link of metadata?.topicLinks||[])links.append(button(`${link.type==='requires'?'Needed for practice':link.type==='recommended'?'Recommended':'Related'}: ${link.targetTitle||link.toId}`,()=>openSubject(link.targetSubjectId,link.toId),{title:link.reason||''}));
    if(links.childNodes.length)lesson.append(links);
    renderResources(lesson,detail,{h,button});
    const missing=(detail.assignments||[]).some(assignment=>assignment.completionEvidence==='observed-success')?detail.actionAvailability?.unmetPrerequisites||[]:[];
    if(missing.length)lesson.append(h('aside',{class:'th-requirement'},h('p',{text:'Reading and responses are open. Before confirming practical success:'}),...missing.map(item=>button(item.targetTitle||item.targetId,()=>openSubject(item.targetSubjectId,item.targetId),{title:item.reason}))));
    for(const assignment of detail.assignments||[])renderWork(lesson,assignment,detail,p,{h,markdown,button});
    layout.append(outline,lesson);root.append(layout,button('Reset this Quest',()=>reset('subject',selected.courseId)));
    return true;
  }
  function renderResources(host,detail,{h,button}) {
    const ids=arr(detail.unit.resourceIds).slice(0,64);
    if(!ids.length)return;
    const disclosure=h('details',{class:'th-web-disclosure th-web-resources'},h('summary',{text:'Optional resources'}),h('p',{text:'These files are supplied practice inputs. Downloads do not execute or complete work; the written lesson remains available.'}));
    const origin=capture(), lifetime={active:true};disposers.push(()=>{lifetime.active=false;});
    for(const id of ids) {
      const item=arr(detail.resources).find(record=>record.id===id), row=h('section',{'aria-label':'Practice resource'}), status=h('p',{role:'status'});
      let url;
      try {
        url=new URL(item?.url,location.origin);
        if(!item || !item.unitIds?.includes(detail.unit.id) || url.origin!==location.origin || url.username || url.password || /\.(html?|m?js|svg)$/i.test(url.pathname) || url.search || url.hash || !/^\/static\/treehouse\/(fixtures|projects)\/[a-z0-9][a-z0-9/._-]*$/.test(url.pathname) || url.pathname.includes('..') || !['data','image','download'].includes(item.kind) || !['application/json','text/plain','image/png','application/zip','application/octet-stream'].includes(item.mime) || !Number.isSafeInteger(item.bytes) || item.bytes<1 || item.bytes>8*1024*1024 || !/^sha256:[a-f0-9]{64}$/.test(item.digest))throw new Error();
      }catch {row.append(h('p',{text:'Resource unavailable. Use the supplied written input.'}));disclosure.append(row);continue;}
      row.append(h('p',{text:`${item.id} · ${item.mime} · ${item.bytes} bytes · ${item.provenance?.status||'source-only'}`}));
      const load=async copy=>{
        if(!current(origin)||!lifetime.active)return;
        status.textContent='Checking resource…';
        const abort=new AbortController();disposers.push(()=>abort.abort());
        try {
          const result=await fetch(url.href,{credentials:'same-origin',redirect:'error',signal:abort.signal});
          if(!result.ok)throw new Error();
          // Fetch exposes decoded bytes; an encoded Content-Length counts wire bytes.
          const encoding=String(result.headers.get('Content-Encoding')||'identity').trim().toLowerCase();
          const declared=Number(result.headers.get('Content-Length'));if((!encoding||encoding==='identity') && declared && declared!==item.bytes)throw new Error();
          // Limit streaming reads even when a server omits/misstates its length.
          const reader=result.body.getReader(), chunks=[];let length=0;
          try {while(true){const {value,done}=await reader.read();if(done)break;length+=value.length;if(length>item.bytes)throw new Error();chunks.push(value);}}finally {await reader.cancel();}
          if(length!==item.bytes)throw new Error();
          const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
          const digest='sha256:'+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');
          if(digest!==item.digest)throw new Error();
          if(!current(origin)||!lifetime.active||!host.isConnected)return;
          if(copy) {
            const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
            if(!navigator.clipboard?.writeText)throw new Error();
            await navigator.clipboard.writeText(text);
            if(current(origin)&&lifetime.active)status.textContent='Resource copied. Review it before local use.';
          } else {
            const blobUrl=URL.createObjectURL(new Blob([bytes],{type:item.mime}));
            const link=h('a',{href:blobUrl,download:url.pathname.split('/').pop(),text:'Download checked file'});
            // Blob is download-only, never iframe/script/HTML preview.
            row.append(link);link.click();disposers.push(()=>URL.revokeObjectURL(blobUrl));
            status.textContent='Checked download prepared. Open it only in your chosen local tool.';
          }
        }catch {if(current(origin)&&lifetime.active)status.textContent='Resource unavailable or changed. Use the supplied written input; no work was completed.';}
      };
      if(['text/plain','application/json'].includes(item.mime))row.append(button('Copy resource',()=>load(true)));
      row.append(button('Download resource',()=>load(false)),status);disclosure.append(row);
    }
    host.append(disclosure);
  }
  function renderWork(host, assignment, detail, p, {h,markdown,button}) {
    const submission=detail.submissions?.[assignment.id], practical=assignment.completionEvidence==='observed-success';
    const availability=assignment.actionAvailability || (practical?detail.actionAvailability:{status:'available'});
    const form={origin:capture(),accountId:context().ui.snapshot.accountId,sourceId:SYSTEM,packageRevision:revision,resetGeneration:detail.resetGeneration,assignmentRequirementDigest:assignment.requirementDigest,workBase:assignment.contentSource?.contentHead};
    const draftKey=JSON.stringify([form.accountId,SYSTEM,detail.subjectId,assignment.id]);
    const local=drafts.get(draftKey);
    const localMatches=local && local.requirementDigest && local.requirementDigest===assignment.requirementDigest && local.resetGeneration===detail.resetGeneration;
    const serverDraft=submission?.draftAnswer!==undefined && submission?.draftAnswer!==null;
    const serverAnswer=serverDraft?submission.draftAnswer:submission?.answer;
    const serverState=serverDraft?submission?.draftRecoveryStatus:submission?.answerRecoveryStatus;
    let saved=localMatches?local.answer:!local && serverState==='current'?serverAnswer:undefined;
    const recovered=localMatches && local.packageRevision!==revision;
    if(recovered && practical && saved && typeof saved==='object')saved={...saved,outcome:{...saved.outcome,learnerConfirmed:false}};
    const previous=local && !localMatches?local.answer:(!local && serverAnswer!==undefined && serverAnswer!==null && serverState!=='current' && serverState!=='reset'?serverAnswer:undefined);
    const previousSignature=JSON.stringify([previous,local?.requirementDigest,local?.resetGeneration,serverState]);
    const response=h('textarea',{rows:4,'aria-label':'Your response','data-treehouse-focus':`${assignment.id}:response`});response.value=typeof saved==='string'?saved:saved?.response||'';
    const work=h('section',{class:'th-web-work'},h('h3',{text:'Your work'}));
    if(!assignment.promptInBody)work.append(markdown(assignment.prompt));
    work.append(h('p',{text:'Success conditions'}),...arr(assignment.successConditions).map(text=>h('p',{text})));
    if(assignment.promptInBody)work.append(h('details',{},h('summary',{text:'Repeat task prompt'}),markdown(assignment.prompt)));
    work.append(h('p',{class:'th-evidence-label',text:(p.requiredAssignmentIds||[]).includes(assignment.id)?'Required · self-report':'Elective · self-report'}));
    if(assignment.verificationLimit)work.append(h('details',{},h('summary',{text:'What this records'}),h('p',{text:assignment.verificationLimit})));
    if(recovered)work.append(h('p',{role:'status',text:'Recovered unsent work for an equivalent requirement. Review it before saving; unsaved text is lost on page reload.'}));
    work.append(response);
    if(practical && detail.stale)work.append(h('p',{role:'status',text:'Progress changed. Refresh this lesson before confirming practical success.'}),button('Refresh lesson',async()=>{await unitDetail(detail.subjectId,detail.unit.id,true);rerender();}));
    let outcome,confirmed,evidence;
    if(practical) {
      outcome=h('select',{'aria-label':'Practical outcome','data-treehouse-focus':`${assignment.id}:outcome`},h('option',{value:'blocked',text:'Not configured / blocked'}),h('option',{value:'failed',text:'Attempt failed'}),h('option',{value:'success',text:'Outcome achieved'}));outcome.value=saved?.outcome?.status||'blocked';
      confirmed=h('input',{type:'checkbox','data-treehouse-focus':`${assignment.id}:confirmed`});confirmed.checked=!!saved?.outcome?.learnerConfirmed;confirmed.disabled=outcome.value!=='success';
      evidence=h('textarea',{rows:3,'aria-label':'Outcome evidence','data-treehouse-focus':`${assignment.id}:evidence`});evidence.value=saved?.outcome?.evidenceText||'';
      work.append(h('label',{},h('span',{text:'Practical outcome'}),outcome),h('label',{},confirmed,h('span',{text:'I achieved the stated outcome'})),h('label',{},h('span',{text:'Actual evidence or failure details'}),evidence));
    }
    const answer=()=>practical?topicPracticalAnswer(response.value,outcome.value,confirmed.checked,evidence.value):response.value;
    const remember=()=>{if(context().ui.snapshot.accountId!==form.accountId || catalogue()?.sourceId!==form.sourceId)return;drafts.set(draftKey,{answer:answer(),accountId:form.accountId,sourceId:SYSTEM,courseId:detail.subjectId,assignmentId:assignment.id,packageRevision:form.packageRevision,requirementDigest:assignment.requirementDigest,resetGeneration:form.resetGeneration});};
    if(previous!==undefined && dismissedPrevious.get(draftKey)?.signature!==previousSignature) {
      const oldText=typeof previous==='string'?previous:previous?.response||'';
      const old=h('textarea',{rows:4,readonly:true,'aria-label':'Previous answer',value:oldText});old.value=oldText;
      work.append(h('details',{class:'th-web-previous'},h('summary',{text:'Previous answer · review against this task'}),h('p',{text:'This answer belongs to changed, unknown or reset requirements. It does not complete this task. Copy it to review, or discard this recovery copy.'}),old,
        button('Copy previous answer',()=>{response.value=oldText;if(practical){outcome.value='blocked';confirmed.checked=false;confirmed.disabled=true;evidence.value=previous?.outcome?.evidenceText||'';}remember();dismissedPrevious.set(draftKey,{courseId:detail.subjectId,signature:previousSignature});rerender();}),
        button('Discard previous answer',()=>{if(local && !localMatches)drafts.delete(draftKey);dismissedPrevious.set(draftKey,{courseId:detail.subjectId,signature:previousSignature});rerender();})));
    }
    response.addEventListener('input',remember);
    for(const input of [outcome,confirmed,evidence].filter(Boolean)) {input.addEventListener('input',remember);input.addEventListener('change',remember);}
    const save=async type=>{
      const origin=form.origin, courseId=detail.subjectId, unitId=detail.unit.id;
      if(!current(origin))throw new Error('This task changed. Reload the lesson and review your previous answer before saving.');
      if(type==='submission.submit' && practical && outcome.value==='success' && (detail.stale || availability?.status==='blocked'))throw new Error('Complete the listed practical prerequisites, then refresh this lesson. Responses and failed attempts remain available.');
      if(type==='submission.submit' && (!response.value.trim() || (practical && outcome.value==='success' && (!confirmed.checked || !evidence.value.trim()))))throw new Error('Provide your response and confirm the achieved outcome with actual evidence.');
      const sentAnswer=answer();
      if(assignment.contentSource&&!form.workBase)throw new Error('Reload this task before saving; its source identity is missing.');
      const result=await command(type,{courseId,assignmentId:assignment.id,assignmentRequirementDigest:form.assignmentRequirementDigest,...(form.workBase?{workBase:form.workBase}:{}),answer:sentAnswer},form);
      if(!result || !current(origin))return;
      if(type==='submission.submit' && JSON.stringify(drafts.get(draftKey)?.answer)===JSON.stringify(sentAnswer))drafts.delete(draftKey);
      await unitDetail(courseId,unitId,true);if(current(origin))rerender();
    };
    const submit=button(practical?'Save attempt':'Submit response',()=>save('submission.submit'),{class:'copal-btn primary'});
    const updateOutcome=()=>{submit.disabled=practical && outcome.value==='success' && (detail.stale || availability?.status==='blocked');if(practical){confirmed.disabled=outcome.value!=='success';if(confirmed.disabled)confirmed.checked=false;}};
    outcome?.addEventListener('change',updateOutcome);updateOutcome();
    work.append(button('Save draft',()=>save('submission.draft')),submit);
    if(submission)work.append(h('p',{role:'status',text:submission.status==='reset'?'Reset · work pending':submission.status==='pending'?'Attempt saved · practical work pending':submission.status==='graded'?'Reviewed work':submission.status==='submitted'?'Submitted · self-reported work':'Draft saved'}));
    if(submission?.attemptHistory?.length)work.append(h('details',{},h('summary',{text:'Attempt history'}),...submission.attemptHistory.map(attempt=>h('p',{text:`Attempt ${attempt.ordinal||''} · ${attempt.status||'saved'}${attempt.completionSatisfied===false?' · Pending':''}`}))));
    const feedback=h('details',{},h('summary',{text:'Feedback and hint'}),h('p',{text:assignment.failureFeedback||''}),h('p',{text:assignment.hint||''}));work.append(feedback);host.append(work);
  }
  return {render,renderAvailability,openRoot,openSubject,openHome,restore,setPrivateMode:value=>{privateMode=!!value;},invalidateContent:()=>{epoch++;navigation++;mutationVersion++;loading=false;roots.clear();subjects.clear();units.clear();error=null;},get location(){return {...selected,privateMode};},rootDetail,subjectDetail,unitDetail,command,reset,sync,dispose:()=>{epoch++;disposeAssets();},get selected(){return {...selected};},get progress(){return progress;}};
}
