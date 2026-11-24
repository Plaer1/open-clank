import { getWorkspaceId } from './workspace.js';
import { currentChatWorkStatus } from './chatWorkStatus.js';
import { openAgentRunInspector } from './agentRunInspector.js';

const SOURCES = [
  ['native_task', 'Chat commitments'], ['native_actor', 'Chat executions'],
  ['native_peer_actor', 'Direct peer executions'],
  ['workflow', 'Session-launched workflows'],
  ['scheduler_routine', 'Routines'], ['scheduler_run', 'Routine executions'],
  ['scheduler_wait', 'Routine waits'],
  ['shell_job', 'Shell jobs'], ['media_job', 'Media jobs'],
  ['memory_job', 'Source-linked memory jobs'],
  ['copal', 'Copal commitments'],
];
const MISSING = ['Other memory jobs'];
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
};

/** A bounded view of server-authorized observations. No owner comes from the UI. */
export function mountTaskWorkload(container) {
  let stopped = false, controller = null, generation = 0, authEpoch = 0, selected = 0, detailController = null;
  let goalController = null, goalGeneration = 0, goalMutation = null;
  const pages = new Map(), pending = new Map();
  const root = node('div', null, 'task-workload');
  const header = node('div', null, 'task-workload-header');
  header.append(node('h3', 'Workload'));
  const refreshButton = node('button', 'Refresh');
  refreshButton.type = 'button';
  header.append(refreshButton);
  const summary = node('p', 'Loading observations…', 'task-workload-summary');
  const chatStatus = node('section', null, 'task-workload-chat-status');
  const content = node('div', null, 'task-workload-sources');
  const detail = node('section', null, 'task-workload-detail');
  root.append(header, summary, chatStatus, detail, content);
  container.append(root);
  const scope = () => ({ chat: window.sessionModule?.getCurrentSessionId?.() || '', workspace: getWorkspaceId() || '', account: document.body?.dataset?.accountId || '', authEpoch });
  const sameScope = (a, b) => a.chat === b.chat && a.workspace === b.workspace && a.account === b.account && a.authEpoch === b.authEpoch;
  const reasonText = reason => ({session_workflows:'Session-launched records only; workspace is shown only when witnessed, and live running state is unverified', more_pages:'More pages available', direct_peer_subset:'Visible direct peers only; descendant coverage is partial', legacy_or_unqualified_excluded:'Older or unqualified records are not shown', source_linked_subset:'Only source-linked capture and review jobs are shown', source_not_requested:'Not requested', source_not_supplied:'Not available', native_paging_unavailable:'This chat does not support paged observations', native_paging_or_peer_unavailable:'Chat observations are unavailable', native_observation_unavailable:'Chat observations are unavailable'})[reason] || (reason ? 'Source unavailable' : '');
  const refKey = row => { const ref = row.sourceRef; return ref && JSON.stringify([ref.kind, ref.sessionId || '', ref.nativeId, ref.executionRevision ?? null, ref.taskId || '', ref.runId || '']); };
  const visible = () => !stopped && document.visibilityState !== 'hidden' && root.isConnected;
  function render() {
    if (stopped) return;
    const count = category => [...pages.values()].reduce((n, page) => n + page.items.filter(row => row.displayCategory === category).length, 0);
    summary.textContent = `Observed: ${count('commitment')} commitments · ${count('execution')} executions · ${count('routine')} routines. Counts cover loaded pages only.`;
    chatStatus.replaceChildren(node('h4', 'Current chat · loaded observations'));
    for (const item of currentChatWorkStatus(pages, scope(), generation)) {
      const counts = item.statuses.length ? item.statuses.map(value => `${value.status}: ${value.count}`).join(' · ') : '0 loaded';
      const time = item.source === 'goal' && pages.get('goal')?.observedAt ? ` · observed ${new Date(pages.get('goal').observedAt).toLocaleTimeString()}` : '';
      chatStatus.append(node('p', `${item.label}: ${item.availability === 'not_loaded' ? 'Not loaded' : item.availability}${['unavailable','not_loaded','stale'].includes(item.availability) ? '' : ' · ' + counts}${time}`));
      for (const goal of item.goals || []) chatStatus.append(node('p', `${goal.role}: ${goal.id} · revision ${goal.revision} · ${goal.status}`));
    }
    chatStatus.append(node('p', 'Native pages cover the root session; memory jobs are a source-linked subset.'));
    content.replaceChildren();
    for (const [source, label] of SOURCES) {
      const page = pages.get(source) || {availability: 'not_loaded', reason: 'source_not_requested', items: []};
      const section = node('section', null, 'task-workload-source');
      section.append(node('h4', label));
      const time = page.observedAt ? ` · as of ${new Date(page.observedAt).toLocaleString()}` : '';
      section.append(node('p', `${page.availability === 'not_loaded' ? 'Not loaded' : page.availability}${page.reason ? ' · ' + reasonText(page.reason) : ''}${time}`, 'task-workload-coverage'));
      for (const row of page.items) {
        const item = node('div', null, 'task-workload-row');
        item.append(node('strong', row.title || 'Untitled'), node('span', row.nativeStatus || 'Unknown'));
        if (source === 'workflow') {
          const witnessed=row.workspaceAvailability==='available' && row.workspaceId===scope().workspace;
          item.append(node('small',witnessed ? 'Recorded launch workspace matches this selection' : 'Launch workspace unverified'));
          if (witnessed && Number.isSafeInteger(page.mapRevision)) {
            const inspect=node('button','View workflow steps');
            inspect.type='button';
            inspect.addEventListener('click',()=>void inspectWorkflow(row,page,inspect));
            item.append(inspect);
          }
        }
        if ((source === 'native_actor' || source === 'native_peer_actor') && row.sourceRef?.nativeId) item.append(node('small', `Actor ID: ${row.sourceRef.nativeId}`));
        if ((source === 'native_actor' || source === 'native_peer_actor') && row.sourceRef?.nativeId && row.sourceRef?.sessionId && Number.isSafeInteger(row.sourceRef.executionRevision)) {
          const inspect = node('button', 'Inspect'); inspect.type = 'button';
          inspect.addEventListener('click', () => void inspectActor(row, page)); item.append(inspect);
        }
        if (source === 'native_actor' && row.actorDisplay?.renameAvailable === false) item.append(node('small', 'Rename unavailable: original owner is unknown'));
        if (source === 'native_actor' && row.actorDisplay?.renameAvailable === true && row.sourceRef?.nativeId && row.sourceRef?.sessionId) {
          const rename=node('button','Rename'); rename.type='button';
          rename.addEventListener('click', async () => {
            const captured=scope(), epoch=generation, ref=row.sourceRef, display=row.actorDisplay;
            const label=window.prompt('Worker display name',display.label);
            if (label===null || !visible() || epoch!==generation || !sameScope(captured,scope())) return;
            rename.disabled=true;
            try {
              const result=await fetch(`/api/orchestration/${encodeURIComponent(captured.chat)}/actors/${encodeURIComponent(ref.nativeId)}/display-name`, {
                method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({label,executionRevision:ref.executionRevision,displayRevision:display.revision,actorSessionID:ref.sessionId,mapRevision:page.mapRevision}),signal:controller.signal,
              });
              const value=await result.json();
              if (!visible() || epoch!==generation || !sameScope(captured,scope())) return;
              if (value.state==='conflict') window.alert('Worker changed. Refresh and try again.');
              else if (value.state!=='applied') window.alert('The rename outcome is unknown or unavailable. Refresh to see the current name.');
              refresh();
            } catch (error) {
              if (error.name!=='AbortError' && visible() && epoch===generation && sameScope(captured,scope())) {
                window.alert('The rename outcome is unknown. Refresh to see the current name.'); refresh();
              }
            } finally { rename.disabled=false; }
          });
          item.append(rename);
        }
        if (row.linkCoverage === 'unknown') item.append(node('small', source.endsWith('_job') ? 'Related task not verified' : 'Related execution not observed'));
        for (const link of row.links || []) item.append(node('small', `${link.kind}: ${link.target?.kind || 'source'} ${link.target?.nativeId || ''}`));
        section.append(item);
      }
      if (page.nextCursor) {
        const more = node('button', 'Load more'); more.type = 'button';
        more.disabled = pending.has(source);
        more.addEventListener('click', () => void (source === 'native_peer_actor' ? loadPeers(page.nextCursor) : source === 'workflow' ? loadWorkflows(page.nextCursor) : load(source, page.nextCursor))); section.append(more);
      }
      content.append(section);
    }
    const unavailable = node('p', `${MISSING.join(', ')}: unavailable. Their absence here does not mean they are empty or complete.`, 'task-workload-coverage');
    content.append(unavailable);
  }
  // The one worker inspector (agentRunInspector.js) lives in this detail region; the shared chat-visible panel hosts this
  // Workload, so there is no second inspector surface. Relations come from the pages already loaded here.
  async function inspectActor(row, page) {
    detailController?.abort(); detailController = new AbortController();
    const abortOfThis = detailController;
    const ticket = ++selected, epoch = generation, captured = scope();
    const current = () => visible() && ticket === selected && epoch === generation && sameScope(captured, scope());
    const mapRevision = page.mapRevision;
    detail.replaceChildren(node('p', 'Loading worker details…'));
    let renderAccounting = null;
    try { renderAccounting = (await import('./chatRenderer.js')).renderActorAccounting; } catch { /* inspector shows "unavailable" */ }
    if (!current()) return;
    const relations = () => ({
      rootSessionId: pages.get('native_actor')?.nativeScope?.rootSessionId,
      actors: [...(pages.get('native_actor')?.items || []).map(item => ({ row: item, mapRevision: pages.get('native_actor')?.mapRevision, peer: false })),
        ...(pages.get('native_peer_actor')?.items || []).map(item => ({ row: item, mapRevision: pages.get('native_peer_actor')?.mapRevision, peer: true }))],
      tasks: pages.get('native_task')?.items || [], workflows: pages.get('workflow')?.items || [],
    });
    detail.replaceChildren();
    const view = openAgentRunInspector({ container: detail, doc: document, scope: { chat: captured.chat, mapRevision }, row, relations, current,
      renderAccounting,
      sendFollowUp: async args => (await import('./chat.js')).submitActorFollowUp(args),
      onSelectActor: entry => void inspectActor(entry.row, { mapRevision: entry.mapRevision }),
      onReturnToChat: () => document.getElementById('message')?.focus?.(),
      onRefresh: () => refresh(),
      onClose: () => { selected++; abortOfThis.abort(); } });
    abortOfThis.signal.addEventListener('abort', () => view.dispose());
  }
  async function inspectWorkflow(row,page,opener) {
    detailController?.abort(); detailController=new AbortController();
    const controllerForView=detailController, ticket=++selected, epoch=generation, captured=scope();
    const current=()=>visible() && !controllerForView.signal.aborted && ticket===selected && epoch===generation && sameScope(captured,scope());
    const close=node('button','Close details'); close.type='button';
    close.addEventListener('click',()=>{
      controllerForView.abort();
      detail.replaceChildren();
      if (opener.isConnected) opener.focus();
    });
    const back=node('button','Return to chat'); back.type='button';
    back.addEventListener('click',()=>{ if (current()) document.getElementById('message')?.focus?.(); });
    const status=node('p','Loading workflow steps…','task-workload-coverage');
    status.setAttribute('role','status');
    const results=node('div',null,'task-workload-workflow-steps');
    detail.replaceChildren(node('h4','Workflow steps'),close,back,status,results);
    const ref=row.sourceRef, query=new URLSearchParams({run_id:ref.nativeId,map_revision:String(page.mapRevision)});
    try {
      const result=await fetch(`/api/orchestration/${encodeURIComponent(captured.chat)}/workflow-observation?${query}`,
        {credentials:'same-origin',cache:'no-store',signal:controllerForView.signal});
      if (!result.ok) throw new Error();
      const reading=await result.json(), observed=reading.structure, source=reading.scope;
      if (!current()) return;
      if (reading.version!==1 || source?.hostChatId!==captured.chat || source.rootSessionId!==ref.sessionId
          || source.mapRevision!==page.mapRevision || source.workerGeneration!==page.nativeScope?.workerGeneration
          || source.mappingRevision!==page.nativeScope?.mappingRevision || observed?.runId!==ref.nativeId
          || observed.sessionId!==ref.sessionId || !Array.isArray(observed.nodes)) throw new Error();
      status.textContent=observed.coverage==='unavailable'
        ? `${observed.status} · The current worker has no recorded structure for this run.`
        : `${observed.status} · Partial coverage of the current worker's recorded structure.`;
      const phases=new Map();
      for (const entry of observed.nodes) {
        if (entry.kind==='phase') {
          const steps=node('ul');
          phases.set(entry.id,steps);
          results.append(node('h5',entry.title),steps);
          continue;
        }
        const label=[entry.title,entry.state,entry.actorId ? `Actor ${entry.actorId} in ${ref.sessionId}` : null].filter(Boolean).join(' · ');
        const phase=phases.get(entry.phaseId);
        if (phase) phase.append(node('li',label));
        else results.append(node('p',label));
      }
      if (observed.coverage==='bounded') results.append(node('p','Additional steps exceed this view’s observation limit.'));
    } catch (error) {
      if (current() && error.name!=='AbortError') status.textContent='Workflow structure unavailable for the current chat and worker.';
    }
  }

  async function load(source, cursor = null, epoch = generation, captured = scope()) {
    if (!visible() || !controller || epoch !== generation || pending.has(source)) return;
    if (cursor && pages.get(source)?.nextCursor !== cursor) return;
    const ticket = {}; pending.set(source, ticket); render();
    const native = source.startsWith('native_');
    const job = source === 'shell_job' || source === 'media_job' || source === 'memory_job';
    if (source === 'copal' && !captured.workspace) { pending.delete(source); pages.set(source, {availability:'unavailable', reason:'Select a workspace', items:[]}); render(); return; }
    if ((native || job) && !captured.chat) { pending.delete(source); pages.set(source, {availability:'unavailable', reason:'Select a chat', items:[]}); render(); return; }
    const query = new URLSearchParams({limit:'50'});
    if (cursor) query.set('cursor', cursor);
    if (!native && captured.workspace) query.set('workspace_id', captured.workspace);
    const path = source === 'copal' ? '/api/copal/workload/tasks' : job ? `/api/orchestration/${encodeURIComponent(captured.chat)}/workload-jobs/${source}`
      : native ? `/api/orchestration/${encodeURIComponent(captured.chat)}/observations/${source}` : `/api/tasks/observations/${source}`;
    try {
      const response = await fetch(`${path}?${query}`, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!response.ok) throw new Error(`Unavailable (${response.status})`);
      const data = await response.json();
      if (!visible() || epoch !== generation || !sameScope(captured, scope())) return;
      const page = data.sources?.[source];
      if (!page || !Array.isArray(page.items)) throw new Error('Invalid observation page');
      if (cursor && native && pages.get(source)?.nativeScope?.rootSessionId !== data.scope?.rootSessionId)
        throw new Error('Native observation scope changed');
      const previous = cursor ? pages.get(source)?.items || [] : [];
      const seen = new Set(), items = [];
      for (const item of [...previous, ...page.items]) {
        const key = refKey(item);
        if (!key) throw new Error('Invalid source identity');
        if (!seen.has(key)) { seen.add(key); items.push(item); }
      }
      pages.set(source, {...page, items, mapRevision:data.scope?.mapRevision, requestScope:captured, requestEpoch:epoch,
        nativeScope:native ? {hostChatId:data.scope?.hostChatId, rootSessionId:data.scope?.rootSessionId, mappingRevision:data.scope?.mappingRevision} : undefined});
      const peers=pages.get('native_peer_actor');
      if (native && peers?.nativeScope && (peers.nativeScope.rootSessionId!==data.scope?.rootSessionId || peers.nativeScope.mappingRevision!==data.scope?.mappingRevision))
        pages.set('native_peer_actor',{availability:'unavailable',reason:'native_observation_unavailable',items:[],requestScope:captured,requestEpoch:epoch});
      const workflows=pages.get('workflow');
      if (native && workflows?.nativeScope && (workflows.nativeScope.rootSessionId!==data.scope?.rootSessionId || workflows.nativeScope.mappingRevision!==data.scope?.mappingRevision))
        pages.set('workflow',{availability:'unavailable',reason:'native_observation_unavailable',items:[],requestScope:captured,requestEpoch:epoch});
    } catch (error) {
      if (error.name === 'AbortError' || stopped || epoch !== generation || !sameScope(captured, scope())) return;
      if (cursor && pages.has(source)) pages.set(source, {...pages.get(source), reason:'page_read_failed'});
      else pages.set(source, {availability:'unavailable', reason:'read_failed', items:[]});
    } finally {
      if (pending.get(source) === ticket) pending.delete(source);
    }
    if (epoch === generation && sameScope(captured, scope())) render();
  }
  async function loadWorkflows(cursor=null, epoch=generation, captured=scope()) {
    const source='workflow';
    if (!visible() || !controller || !captured.chat || !captured.account || epoch!==generation || pending.has(source)) return;
    if (cursor && pages.get(source)?.nextCursor!==cursor) return;
    const ticket={}; pending.set(source,ticket); render();
    try {
      const query=new URLSearchParams(); if(cursor)query.set('cursor',cursor);
      const response=await fetch(`/api/orchestration/${encodeURIComponent(captured.chat)}/workflow-observation?${query}`,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if(!response.ok)throw new Error('workflow unavailable');
      const data=await response.json(); if(!visible() || epoch!==generation || !sameScope(captured,scope()))return;
      const native=data?.scope,page=data?.workflows,prior=cursor ? pages.get(source) : null,rootPage=pages.get('native_actor');
      if(data?.version!==1 || native?.hostChatId!==captured.chat || typeof native.rootSessionId!=='string' || !Number.isSafeInteger(native.mappingRevision) || !Number.isSafeInteger(native.workerGeneration) || page?.availability!=='partial' || !Number.isSafeInteger(page.observedAt) || !Array.isArray(page.items) || page.items.length>32 ||
          (rootPage?.nativeScope && (rootPage.nativeScope.rootSessionId!==native.rootSessionId || rootPage.nativeScope.mappingRevision!==native.mappingRevision)) ||
          (prior && (prior.nativeScope?.rootSessionId!==native.rootSessionId || prior.nativeScope?.mappingRevision!==native.mappingRevision || prior.nativeScope?.workerGeneration!==native.workerGeneration))) throw new Error('workflow scope changed');
      const items=prior ? [...prior.items] : [],seen=new Set(items.map(refKey));
      for(const row of page.items) { const key=refKey(row); if(row?.sourceRef?.kind!=='workflow' || row.sourceRef.sessionId!==native.rootSessionId || !key || seen.has(key))throw new Error('invalid workflow'); seen.add(key);items.push(row); }
      pages.set(source,{...page,items,mapRevision:native.mapRevision,requestScope:captured,requestEpoch:epoch,nativeScope:{hostChatId:native.hostChatId,rootSessionId:native.rootSessionId,mappingRevision:native.mappingRevision,workerGeneration:native.workerGeneration}});
    } catch(error) { if(error.name!=='AbortError' && !stopped && epoch===generation && sameScope(captured,scope())) pages.set(source,{availability:'unavailable',reason:'native_observation_unavailable',items:[],requestScope:captured,requestEpoch:epoch}); }
    finally { if(pending.get(source)===ticket)pending.delete(source); }
    if(epoch===generation && sameScope(captured,scope()))render();
  }
  async function loadPeers(cursor=null, epoch=generation, captured=scope()) {
    const source='native_peer_actor';
    if (!visible() || !controller || !captured.chat || !captured.account || epoch!==generation || pending.has(source)) return;
    if (cursor && pages.get(source)?.nextCursor!==cursor) return;
    const ticket={};pending.set(source,ticket);render();
    const query=new URLSearchParams();if(cursor)query.set('cursor',cursor);
    try {
      const response=await fetch(`/api/orchestration/${encodeURIComponent(captured.chat)}/peer-observation?${query}`,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if(!response.ok)throw new Error('peer unavailable');
      const data=await response.json();
      if(!visible() || epoch!==generation || !sameScope(captured,scope()))return;
      const native=data?.scope,page=data?.directPeers,prior=cursor ? pages.get(source) : null,rootPage=pages.get('native_actor');
      if(data?.version!==1 || native?.hostChatId!==captured.chat || typeof native.rootSessionId!=='string' || !Number.isSafeInteger(native.mappingRevision) ||
          !Number.isSafeInteger(native.workerGeneration) || page?.availability!=='partial' || !Number.isSafeInteger(page.observedAt) || !Array.isArray(page.items) || page.items.length>4 ||
          (rootPage?.nativeScope && (rootPage.nativeScope.rootSessionId!==native.rootSessionId || rootPage.nativeScope.mappingRevision!==native.mappingRevision)) ||
          (prior && (prior.nativeScope?.rootSessionId!==native.rootSessionId || prior.nativeScope?.mappingRevision!==native.mappingRevision || prior.nativeScope?.workerGeneration!==native.workerGeneration))) throw new Error('peer scope changed');
      const items=prior ? [...prior.items] : [], sessions=new Set(prior?.nativeScope?.peerSessions || []),seen=new Set(items.map(refKey));
      for(const peer of page.items) {
        if(typeof peer?.sessionId!=='string' || !peer.sessionId || peer.parentSessionId!==native.rootSessionId || sessions.has(peer.sessionId) || !Array.isArray(peer.actors?.items))throw new Error('invalid peer');
        sessions.add(peer.sessionId);
        for(const row of peer.actors.items) {
          if(row?.sourceRef?.kind!=='native_actor' || row.sourceRef.sessionId!==peer.sessionId)throw new Error('invalid peer actor');
          const key=refKey(row);if(!key || seen.has(key))throw new Error('duplicate peer actor');seen.add(key);items.push(row);
        }
      }
      pages.set(source,{availability:'partial',reason:'direct_peer_subset',items,observedAt:page.observedAt,
        ...(page.nextCursor ? {nextCursor:page.nextCursor}:{}),mapRevision:native.mapRevision,requestScope:captured,requestEpoch:epoch,
        nativeScope:{hostChatId:native.hostChatId,rootSessionId:native.rootSessionId,mappingRevision:native.mappingRevision,workerGeneration:native.workerGeneration,peerSessions:[...sessions]}});
    } catch(error) {
      if(error.name==='AbortError' || stopped || epoch!==generation || !sameScope(captured,scope()))return;
      pages.set(source,{availability:'unavailable',reason:'native_observation_unavailable',items:[],requestScope:captured,requestEpoch:epoch});
    } finally {if(pending.get(source)===ticket)pending.delete(source);}
    if(epoch===generation && sameScope(captured,scope()))render();
  }
  async function loadGoal(epoch, captured) {
    if (!visible() || !captured.chat || !captured.account || !controller) return;
    goalController?.abort(); goalController = new AbortController(); const goalTicket = ++goalGeneration;
    try {
      const response = await fetch(`/api/orchestration/${encodeURIComponent(captured.chat)}/goal-observation`,
        {credentials:'same-origin',cache:'no-store',signal:goalController.signal});
      if (!response.ok) throw new Error('Goal observation unavailable');
      const value = await response.json();
      if (!visible() || goalTicket !== goalGeneration || epoch !== generation || !sameScope(captured, scope())) return;
      if (!value || !['available','partial'].includes(value.state) || !Number.isSafeInteger(value.observedAt) || value.observedAt < 0 || !Array.isArray(value.queue) || value.queue.length > 32)
        throw new Error('Invalid goal observation');
      pages.set('goal', {availability:value.state, value, items:[], observedAt:value.observedAt, requestScope:captured, requestEpoch:epoch});
    } catch (error) {
      if (error.name === 'AbortError' || stopped || goalTicket !== goalGeneration || epoch !== generation || !sameScope(captured, scope())) return;
      pages.set('goal', {availability:'unavailable', items:[], requestScope:captured, requestEpoch:epoch});
    }
    if (epoch === generation && sameScope(captured, scope())) render();
  }
  function refresh() {
    if (!visible()) return;
    controller?.abort(); goalController?.abort(); goalGeneration++; goalMutation = null; detailController?.abort(); selected++; detail.replaceChildren(); controller = new AbortController(); generation++;
    pages.clear(); pending.clear(); render();
    const captured = scope(), epoch = generation;
    for (const [source] of SOURCES) if(source!=='native_peer_actor' && source!=='workflow') void load(source, null, epoch, captured);
    void loadPeers(null,epoch,captured);
    void loadWorkflows(null,epoch,captured);
    void loadGoal(epoch, captured);
  }
  function changed() { controller?.abort(); goalController?.abort(); goalGeneration++; goalMutation = null; detailController?.abort(); selected++; detail.replaceChildren(); generation++; pages.clear(); pending.clear(); if (visible()) refresh(); else render(); }
  function authChanged() { authEpoch++; changed(); }
  function goalChanged(event) {
    const value=event.detail, captured=scope();
    if (!visible() || !value || value.chat!==captured.chat || value.account!==captured.account || !value.token) return;
    if (value.state==='issued') {
      goalController?.abort(); goalGeneration++; goalMutation={token:value.token,scope:captured,epoch:generation};
      pages.set('goal',{availability:'stale',items:[],requestScope:captured,requestEpoch:generation}); render();
    } else if (value.state==='committed' && goalMutation?.token===value.token && goalMutation.epoch===generation && sameScope(goalMutation.scope,captured)) {
      goalMutation=null; void loadGoal(generation,captured);
    }
  }
  refreshButton.addEventListener('click', refresh);
  document.addEventListener('odysseus:session-selected', changed);
  document.addEventListener('openclank:goal-observation-change', goalChanged);
  document.addEventListener('openclank:auth-context-changed', authChanged);
  window.addEventListener('openclank:auth-context-changed', authChanged);
  window.addEventListener('workspace-change', changed);
  document.addEventListener('visibilitychange', changed);
  refresh();
  return { dispose() {
    stopped = true; generation++; goalGeneration++; selected++; controller?.abort(); goalController?.abort(); detailController?.abort();
    document.removeEventListener('odysseus:session-selected', changed);
    document.removeEventListener('openclank:goal-observation-change', goalChanged);
    document.removeEventListener('openclank:auth-context-changed', authChanged);
    window.removeEventListener('openclank:auth-context-changed', authChanged);
    window.removeEventListener('workspace-change', changed);
    document.removeEventListener('visibilitychange', changed);
    root.remove();
  }};
}
