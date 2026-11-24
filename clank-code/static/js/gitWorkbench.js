import { createOpenClankWindow } from './copal/windows.js';
import { filesFacadeClient } from './filesFacadeClient.js';

export async function loadGitWorkbenchStatus({ rootRef, chatID, isCurrent, signal }) {
  const post = async (path, body) => {
    const response = await fetch('/api/git-review/' + path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    return response.ok ? response.json() : null;
  };
  if (!rootRef || !isCurrent()) return null;
  const repository = await post('folder-state', { rootRef });
  if (!isCurrent()) return null;
  if (repository?.state === 'none' || (repository?.state === 'repository'
      && (repository.kind !== 'repository' || repository.root_relative !== '.'))) {
    throw new Error('Choose the root of a supported Git repository.');
  }
  if (repository?.state !== 'repository') throw new Error('Git repository information is unavailable for this folder.');
  if (!chatID) throw new Error('Open the managed chat for this repository to use the Git workbench.');
  const status = await post('read', { rootRef, chatID, operation: 'status' });
  if (!isCurrent()) return null;
  if (status?.state !== 'ready' || status.comparison !== 'raw' || !status.snapshot || !Array.isArray(status.items)) {
    throw new Error('Git workbench requires an active managed chat rooted in this repository.');
  }
  return status;
}

let workbenchWindow = null;
let closeWorkbench = null;
let openingWorkbench = 0;

export async function openGitWorkbenchWindow({
  rootRef, name, chatID, account, generation, getGeneration, isCurrent,
  isStatusCurrent = isCurrent, trigger, setStatus, signal,
}) {
  const opening = ++openingWorkbench;
  const isOpening = () => opening === openingWorkbench && isCurrent();
  let status;
  try {
    status = await loadGitWorkbenchStatus({ rootRef, chatID, isCurrent: isOpening, signal });
  } catch (error) {
    if (isOpening()) setStatus(error.message || 'Git workbench is unavailable.', true);
    return false;
  }
  if (!status || !isOpening()) return false;
  workbenchWindow ||= createOpenClankWindow({
    id: 'git-workbench-window', label: 'Git workbench', subtitle: '', navigationChrome: false,
    onClosed: () => { openingWorkbench += 1; closeWorkbench?.(); },
  });
  closeWorkbench?.();
  const lifetime = new AbortController();
  const statusCurrent = () => !lifetime.signal.aborted && workbenchWindow.visible && isStatusCurrent();
  const current = () => statusCurrent() && isCurrent();
  let picker = null;
  let observedGeneration = generation;
  const choose = async (purpose, access) => {
    const { createResourcePicker } = await import('./copal/resourcePicker.js');
    if (!current()) return null;
    return new Promise(resolve => {
      picker?.destroy();
      let selected = null;
      const candidate = createResourcePicker({
        client: filesFacadeClient, purpose, accountScope: account, generation, getGeneration,
        originWindow: workbenchWindow, isOriginCurrent: current,
        ...(purpose === 'folder' ? { isCompatible: resource => resource.provider === 'host' && resource.kind === 'folder'
          && resource.capabilities?.children === true && (access === 'remote-read' || resource.capabilities?.write === true)
          ? '' : 'Choose an authorized Host folder.' } : {}),
        onSelect: resource => { selected = resource.ref; return true; },
        onClose: cancelled => { if (picker === candidate) picker = null; resolve(cancelled ? null : selected); },
      });
      picker = candidate;
      void candidate.open().then(opened => { if (!opened) candidate.destroy(); }, () => candidate.destroy());
    });
  };
  const view = createGitWorkbench({
    rootRef, chatID, initialStatus: status, isCurrent: current, isStatusCurrent: statusCurrent,
    selectFile: () => choose('snapshot-file'), selectFolder: access => choose('folder', access),
    onPolicyTransition: result => {
      const transition = result?.policyTransition;
      if (!statusCurrent() || transition?.fromGeneration !== generation || !Number.isSafeInteger(transition.toGeneration)
          || transition.toGeneration <= observedGeneration || transition.toGeneration > generation + 2) return;
      observedGeneration = transition.toGeneration;
      view.enterStatusOnly();
      document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
    },
  });
  closeWorkbench = () => {
    lifetime.abort();
    picker?.destroy();
    view.dispose();
    closeWorkbench = null;
  };
  const invalidate = () => { if (!isStatusCurrent()) workbenchWindow.requestClose(); };
  for (const target of [document, window]) {
    target.addEventListener('openclank:auth-context-changed', () => workbenchWindow.requestClose(), { signal: lifetime.signal });
    target.addEventListener('odysseus:session-selected', invalidate, { signal: lifetime.signal });
  }
  window.addEventListener('workspace-change', invalidate, { signal: lifetime.signal });
  document.addEventListener('openclank:file-policy-changed', () => view.enterStatusOnly(), { signal: lifetime.signal });
  workbenchWindow.setSubtitle(name);
  workbenchWindow.body.replaceChildren(view.element);
  workbenchWindow.show(trigger);
  return true;
}

// Explicit raw-byte source control. Files references, not Git paths, authorize mutations.
export function createGitWorkbench({rootRef, chatID, activeFile, activeFileRef, initialStatus, isCurrent, isStatusCurrent, selectFile, selectFolder, onPolicyTransition}) {
  const pane = document.createElement('section'); pane.setAttribute('aria-label', 'Git workbench')
  const heading = document.createElement('strong'); heading.textContent = 'Repository'
  const refresh = document.createElement('button'); refresh.type = 'button'; refresh.textContent = 'Refresh'
  const status = document.createElement('div'); status.setAttribute('role', 'status')
  const list = document.createElement('div'); const detail = document.createElement('pre')
  detail.style.cssText = 'max-height:18rem;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere'
  const review = document.createElement('div'); pane.append(heading, refresh, status, list, detail, review)
  const repositoryReads = document.createElement('div'); pane.append(repositoryReads)
  for (const [operation, label] of [['branches','Branches'], ['log','Commit log'], ['worktrees','Worktrees']]) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label
    button.addEventListener('click', async () => {
      if (issued || !isCurrent()) return
      const turn = ++serial; detail.textContent = `Reading ${label.toLowerCase()}…`
      try {
        const value = await read({operation}); if (turn !== serial || !isCurrent()) return
        if (value.version !== 1 || value.state !== 'ready' || value.operation !== operation || !Array.isArray(value.items) || value.items.length > 100 || typeof value.partial !== 'boolean') throw Error('invalid')
        detail.textContent = value.items.map(item => operation === 'branches' ? `${item.current ? '* ' : '  '}${item.name} · ${item.oid}` : operation === 'log' ? `${item.oid} · ${item.subject}` : `${item.relativePath} · ${item.branch || (item.detached ? 'detached' : 'branch unavailable')} · ${item.headOID}${item.locked ? ' · locked' : ''}${item.prunable ? ' · prunable' : ''}`).join('\n') || '(None)'
        status.textContent = value.partial ? `${label}: partial result.` : `${label}: current observation.`
      } catch { if (turn === serial && isCurrent()) detail.textContent = `${label} unavailable.` }
    }); repositoryReads.append(button)
  }
  let serial = 0, snapshot = null, controller = new AbortController(), selection = activeFileRef || '', folderSelection = '', pending = null, issued = false, statusOnly = false
  const current = () => statusOnly || issued ? isStatusCurrent() : isCurrent()
  const post = async (endpoint, payload) => {
    if (!(endpoint.endsWith('-status') ? isStatusCurrent() : !statusOnly && isCurrent())) throw new Error('Git workbench context changed')
    const response = await fetch(`/api/git-review/${endpoint}`, {method:'POST', signal:controller.signal, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), cache:'no-store'})
    if (!response.ok) throw new Error('Git workbench unavailable')
    return response.json()
  }
  const read = payload => post('read', {rootRef, chatID, ...payload})
  const side = (label, value) => {
    const title = document.createElement('strong'); title.textContent = label
    const body = document.createElement('pre')
    body.style.cssText = detail.style.cssText
    body.textContent = !value.exists ? 'Absent' : `${value.mode || 'unknown mode'} · ${value.size} bytes · SHA-256 ${value.sha256}\n${value.binary ? '(Binary content)' : value.text ?? '(Content unavailable)'}`
    review.append(title, body)
  }
  const renderStatus = value => {
    review.replaceChildren(); const message = document.createElement('div')
    message.textContent = `Operation: ${value.state}.${value.cleanup ? ` Custody: ${value.cleanup}.` : ''}${pending?.kind === 'commit' ? ` Reviewed branch: ${pending.branch}.` : ''} ${value.state === 'pending' || value.state === 'unknown' || value.state === 'status_only' ? 'Check status before taking another action.' : ''}${value.effect ? (pending?.kind === 'checkout' ? ` Outcome: ${value.effect.outcome}; cleanup: ${value.effect.cleanup}.` : pending?.kind === 'push' ? ` Remote: ${value.effect.remote}; cleanup: ${value.effect.cleanup}.` : pending?.kind === 'fetch' ? ` Transfer: ${value.effect.transfer}; ref: ${value.effect.ref}; cleanup: ${value.effect.cleanup}.` : pending?.kind === 'branch' ? ` Ref: ${value.effect.ref}; cleanup: ${value.effect.cleanup}.` : pending?.kind === 'cleanup' ? ` Host retirement: ${value.effect.hostRetirement}; native retirement: ${value.effect.nativeRetirement}; removal: ${value.effect.removal}; child: ${value.effect.child}; branch: ${value.effect.branch}.` : pending?.kind === 'commit' ? ` Tree: ${value.effect.tree}; commit: ${value.effect.commit}; ref: ${value.effect.ref}; cleanup: ${value.effect.cleanup}${value.effect.commitOID ? `; commit ID: ${value.effect.commitOID}` : ''}.` : ` Object: ${value.effect.object}; candidate retained: ${value.effect.candidateRetained ? 'yes' : 'no'}.`) : ''}`
    review.append(message)
    if (pending && (['pending','unknown','status_only','unavailable'].includes(value.state) || ['pending','unknown'].includes(value.cleanup))) {
      const check = document.createElement('button'); check.type = 'button'; check.textContent = 'Check status'
      check.addEventListener('click', async () => {
        check.disabled = true
        try { const result = await post(pending.kind === 'worktree' ? 'worktree-status' : pending.kind === 'checkout' ? 'checkout-status' : pending.kind === 'cleanup' ? 'cleanup-status' : pending.kind === 'commit' ? 'commit-status' : pending.kind === 'branch' ? 'branch-status' : pending.kind === 'fetch' ? 'fetch-status' : pending.kind === 'push' ? 'push-status' : pending.kind === 'discard' ? 'discard-status' : 'index-status', {version:1,requestID:pending.requestID,expectedDigest:pending.expectedDigest,...(pending.kind==='discard'?{chatID,rootRef,fileRef:pending.fileRef}:{})}); if (current()) { await onPolicyTransition?.(result, pending); if (current()) renderStatus(result) } }
        catch { if (current()) renderStatus({state:'unknown'}) }
      }); review.append(check)
    }
  }
  const preview = async (operation, hunkIDs = null, chosenRef = selection) => {
    const turn = ++serial, fileRef = chosenRef, selectedIDs = hunkIDs && [...hunkIDs].sort(); review.replaceChildren(); status.textContent = 'Preparing review…'
    try {
      const result = await post('index-preview', {version:1,chatID,rootRef,fileRef,operation,...(selectedIDs ? {hunkIDs:selectedIDs} : {})})
      if (turn !== serial || !isCurrent()) return
      if (result.state === 'choose_hunks' && selectedIDs?.length === 0) {
        const groups = result.review?.groups
        if (!Array.isArray(groups) || groups.length > 128) throw new Error('Invalid review')
        const selected = [], seen = new Set(); let displayBytes = 0
        for (const group of groups) {
          const validRange = range => Array.isArray(range) && range.length === 2 && range.every(Number.isSafeInteger) && range[0] >= 0 && range[1] >= range[0] && range[1] <= 1000
          if (!group || !/^[0-9a-f]{64}$/.test(group.id) || seen.has(group.id) || !validRange(group.beforeRange) || !validRange(group.afterRange) || typeof group.before !== 'string' || typeof group.after !== 'string') throw new Error('Invalid review')
          seen.add(group.id); displayBytes += new TextEncoder().encode(group.before).length + new TextEncoder().encode(group.after).length
          if (displayBytes > 65536) throw new Error('Invalid review')
          const label = document.createElement('label'), checkbox = document.createElement('input'), text = document.createElement('pre')
          checkbox.type = 'checkbox'; selected.push({id:group.id, checkbox})
          const lines = ([start, end]) => start === end ? `before line ${start + 1}` : end === start + 1 ? `line ${start + 1}` : `lines ${start + 1}–${end}`
          const target = operation === 'stage_hunks' ? 'Saved file' : 'HEAD'
          text.textContent = `Index ${lines(group.beforeRange)} → ${target} ${lines(group.afterRange)}\nBefore:\n${group.before}\nAfter:\n${group.after}`
          label.append(checkbox, text); review.append(label)
        }
        const next = document.createElement('button'); next.type = 'button'; next.textContent = 'Review selected changes'
        next.addEventListener('click', () => { const ids = selected.filter(item => item.checkbox.checked).map(item => item.id); if (ids.length) void preview(operation, ids, fileRef); else status.textContent = 'Select at least one change.' })
        review.append(next); status.textContent = groups.length ? 'Select changes to review. Staging uses saved file bytes, not unsaved editor text.' : 'No text changes to select.'; return
      }
      if (result.state !== 'ready') { status.textContent = 'Review unavailable or the selection changed.'; return }
      pending = {requestID:result.requestID,expectedDigest:result.expectedDigest}
      status.textContent = `${operation.startsWith('stage') ? 'Stage' : 'Unstage'} ${result.displayName}: raw bytes; Git filters and conversion are not run. Staging uses saved file bytes, not unsaved editor text.`
      side('Current index', result.before); side('Proposed index', result.after)
      const confirm = document.createElement('button'); confirm.type = 'button'; confirm.textContent = operation.startsWith('stage') ? 'Stage selection' : 'Unstage selection'
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Cancel review'
      cancel.addEventListener('click', () => { if (!issued) { pending = null; review.replaceChildren(); status.textContent = 'Review cancelled.' } })
      confirm.addEventListener('click', async () => {
        if (issued || turn !== serial || !isCurrent() || pending?.requestID !== result.requestID) return
        confirm.disabled = true; cancel.disabled = true; issued = true; choose.disabled = true; operationChoice.disabled = true; reviewButton.disabled = true; refresh.disabled = true
        try {
          const value = await post('index-confirm', {version:1,chatID,rootRef,fileRef,operation,...(selectedIDs ? {hunkIDs:selectedIDs} : {}),...pending})
          if (!current()) return
          await onPolicyTransition?.(value, pending)
          if (current()) renderStatus(value)
        } catch { if (current()) renderStatus({state:'unknown'}) }
      }, {once:true}); review.append(confirm, cancel)
    } catch { if (turn === serial && isCurrent()) status.textContent = 'Review unavailable.' }
  }
  const commitForm = document.createElement('div'); commitForm.hidden = true
  const message = document.createElement('textarea'); message.setAttribute('aria-label','Commit message'); message.placeholder = 'Commit message'; message.maxLength = 16384
  const field = (label) => { const input = document.createElement('input'); input.setAttribute('aria-label',label); input.placeholder = label; input.maxLength = 256; return input }
  const authorName=field('Author name'), authorEmail=field('Author email'), committerName=field('Committer name'), committerEmail=field('Committer email')
  const separate=document.createElement('details'), summary=document.createElement('summary'); summary.textContent='Use a different committer'; separate.append(summary,committerName,committerEmail)
  commitForm.append(message,authorName,authorEmail,separate);pane.insertBefore(commitForm,status)
  for (const input of [message,authorName,authorEmail,committerName,committerEmail]) input.addEventListener('input',()=>{if(!issued){serial++;pending=null;review.replaceChildren()}})
  separate.addEventListener('toggle',()=>{if(!issued){serial++;pending=null;review.replaceChildren()}})
  const previewCommit = async () => {
    const turn=++serial, author={name:authorName.value,email:authorEmail.value}, committer=separate.open?{name:committerName.value,email:committerEmail.value}:{...author}
    const body={version:1,chatID,rootRef,message:message.value,author,committer}; review.replaceChildren();status.textContent='Preparing complete staged-tree review…'
    try {
      const value=await post('commit-preview',body); if(turn!==serial || !isCurrent())return
      if(value.state==='no_changes'){status.textContent='The staged tree has no changes to commit.';return}
      const r=value.review, identity=x=>x && typeof x.name==='string' && typeof x.email==='string' && Number.isSafeInteger(x.seconds) && typeof x.timezone==='string'
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || typeof value.expectedDigest!=='string' || !Number.isSafeInteger(value.reviewTime) || !r || r.coverage!=='staged_tree_metadata' || r.unsigned!==true || r.hooks!==false || typeof r.message!=='string' || !identity(r.author) || !identity(r.committer) || typeof r.branch!=='string' || typeof r.parentOID!=='string' || !Array.isArray(r.changes) || r.changes.length>65536)throw Error('Invalid review')
      pending={kind:'commit',requestID:value.requestID,expectedDigest:value.expectedDigest,branch:r.branch}; status.textContent='Review the complete staged-tree metadata. The commit is unsigned and hooks are skipped; blob contents are not reviewed here.'
      const utc=x=>{const date=new Date(x.seconds*1000);if(x.timezone!=='+0000' || !Number.isFinite(date.getTime()))throw Error('Invalid review date');return date.toISOString().replace('T',' ').replace('.000Z',' UTC')}
      const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText;summary.textContent=`Branch: ${r.branch}\nParent: ${r.parentOID}\nAuthor: ${r.author.name} <${r.author.email}> · ${utc(r.author)}\nCommitter: ${r.committer.name} <${r.committer.email}> · ${utc(r.committer)}\n\nMessage:\n${r.message}`;review.append(summary)
      if(r.changes.some(change=>!change || typeof change.path!=='string' || !['added','modified','deleted'].includes(change.kind)))throw Error('Invalid change')
      const changes=document.createElement('div'), range=document.createElement('span'), prev=document.createElement('button'), next=document.createElement('button');changes.style.cssText='max-height:18rem;overflow:auto;overflow-wrap:anywhere';prev.type=next.type='button';prev.textContent='Previous changes';next.textContent='Next changes';let page=0
      const renderPage=()=>{changes.replaceChildren();const start=page*100,end=Math.min(start+100,r.changes.length);range.textContent=`Changes ${r.changes.length?start+1:0}–${end} of ${r.changes.length}`;prev.disabled=page===0;next.disabled=end>=r.changes.length;for(const change of r.changes.slice(start,end)){const row=document.createElement('div');const side=x=>x?`${x.mode} ${x.oid}`:'absent';row.textContent=`${change.kind}: ${change.path} · ${side(change.before)} → ${side(change.after)}`;changes.append(row)}}
      prev.addEventListener('click',()=>{if(page>0){page--;renderPage()}});next.addEventListener('click',()=>{if((page+1)*100<r.changes.length){page++;renderPage()}});renderPage();review.append(range,changes,prev,next)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Commit staged tree';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=true;for(const input of [message,authorName,authorEmail,committerName,committerEmail])input.disabled=true
        try {const result=await post('commit-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest,reviewTime:value.reviewTime});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)} catch {if(current())renderStatus({state:'unknown'})}
      },{once:true}); review.append(confirm,cancel)
    } catch {if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Commit review unavailable.'}}
  }
  const previewCleanup = async (targetRef) => {
    const turn=++serial, body={version:1,chatID,rootRef,targetRef}; review.replaceChildren();status.textContent='Preparing worktree review…'
    try {
      const value=await post('cleanup-preview',body);if(turn!==serial || !isCurrent())return
      const r=value.review
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || !value.requestID || typeof value.expectedDigest!=='string' || !value.expectedDigest || !r || typeof r.targetPath!=='string' || typeof r.branch!=='string' || typeof r.headOID!=='string' || r.clean!==true || r.branchRetained!==true || r.historyRetained!==true || !Number.isSafeInteger(r.nativeSessions) || r.nativeSessions<0 || !Number.isSafeInteger(r.nativeActors) || r.nativeActors<0 || !Array.isArray(r.hostScopes) || r.hostScopes.length>512 || r.hostScopes.some(x=>!x || typeof x.chatID!=='string' || !x.chatID))throw Error('Invalid cleanup review')
      pending={kind:'cleanup',requestID:value.requestID,expectedDigest:value.expectedDigest};status.textContent='Review this clean linked worktree. Its branch and history are retained.'
      const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText;summary.textContent=`Worktree: ${r.targetPath}\nBranch: ${r.branch}\nHEAD: ${r.headOID}\nClean at observation: yes\nBranch retained: yes\nHistory retained: yes\nNative sessions: ${r.nativeSessions} · actors: ${r.nativeActors}`;review.append(summary)
      const scopes=document.createElement('div'), range=document.createElement('span'),prev=document.createElement('button'),next=document.createElement('button');scopes.style.cssText='max-height:18rem;overflow:auto;overflow-wrap:anywhere';prev.type=next.type='button';prev.textContent='Previous chats';next.textContent='Next chats';let page=0
      const renderPage=()=>{scopes.replaceChildren();const start=page*100,end=Math.min(start+100,r.hostScopes.length);range.textContent=`Affected chats ${r.hostScopes.length?start+1:0}–${end} of ${r.hostScopes.length}`;prev.disabled=page===0;next.disabled=end>=r.hostScopes.length;for(const scope of r.hostScopes.slice(start,end)){const row=document.createElement('div');row.textContent=scope.chatID;scopes.append(row)}}
      prev.addEventListener('click',()=>{if(page>0){page--;renderPage()}});next.addEventListener('click',()=>{if((page+1)*100<r.hostScopes.length){page++;renderPage()}});renderPage();review.append(range,scopes,prev,next)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Remove reviewed worktree';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=true
        try {const result=await post('cleanup-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)} catch {if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    } catch {if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Worktree review unavailable.'}}
  }
  const branchForm = document.createElement('div'); branchForm.hidden = true
  const branchName = document.createElement('input'); branchName.setAttribute('aria-label','New branch name'); branchName.placeholder = 'New branch name'; branchName.maxLength = 255; branchForm.append(branchName); pane.insertBefore(branchForm,status)
  branchName.addEventListener('input',()=>{if(!issued){serial++;pending=null;review.replaceChildren()}})
  const previewBranch = async () => {
    const turn=++serial, body={version:1,chatID,rootRef,ref:'refs/heads/'+branchName.value}; review.replaceChildren(); status.textContent='Preparing branch review…'
    try {
      const value=await post('branch-preview',body); if(turn!==serial || !isCurrent())return
      const r=value.review
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || typeof value.expectedDigest!=='string' || !r || r.ref!==body.ref || typeof r.targetOID!=='string' || typeof r.sourceRef!=='string' || r.mustBeAbsent!==true || r.switches!==false)throw Error('Invalid branch review')
      pending={kind:'branch',requestID:value.requestID,expectedDigest:value.expectedDigest};status.textContent='Review the new branch at the current commit. It is created only if the name is absent; the worktree does not switch.'
      const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText;summary.textContent=`New branch: ${r.ref}\nTarget commit: ${r.targetOID}\nSource branch observed at review: ${r.sourceRef}`;review.append(summary)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Create branch';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=branchName.disabled=true
        try {const result=await post('branch-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)} catch {if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    } catch {if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Branch review unavailable.'}}
  }
  const checkoutForm=document.createElement('div');checkoutForm.hidden=true
  const checkoutRef=document.createElement('input');checkoutRef.type='text';checkoutRef.maxLength=277;checkoutRef.setAttribute('aria-label','Full target Git ref');checkoutRef.placeholder='refs/heads/branch or refs/remotes/remote/branch';checkoutForm.append(checkoutRef);pane.insertBefore(checkoutForm,status)
  const previewCheckout=async(operation)=>{
    const turn=++serial,body={version:1,chatID,rootRef,operation,targetRef:checkoutRef.value};review.replaceChildren();status.textContent='Preparing checkout review…'
    try {
      const value=await post('checkout-preview',body);if(turn!==serial || !isCurrent())return
      if(value.state!=='ready' || !value.requestID || !value.expectedDigest || !value.review){status.textContent='Checkout is unavailable, changed, or includes an unsupported file.';return}
      pending={kind:'checkout',requestID:value.requestID,expectedDigest:value.expectedDigest};status.textContent='Review the exact files and target before confirming.'
      const heading=document.createElement('div');heading.textContent=`${operation==='pull'?'Fast-forward to':'Switch to'} ${value.review.targetRef} at ${value.review.targetOID}. Current branch: ${value.review.sourceRef}. Only the reviewed plain Git blob bytes are applied.`;review.append(heading)
      for(const file of value.review.files || []){const title=document.createElement('div'),diff=document.createElement('pre');title.textContent=`${file.path}: ${file.before} → ${file.after}`;diff.textContent=file.diff ?? 'Diff unavailable or too large; exact content hashes remain bound to this review.';review.append(title,diff)}
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent=operation==='pull'?'Apply fast-forward':'Switch branch';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=checkoutRef.disabled=true
        try {const result=await post('checkout-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)}catch{if(current())renderStatus({state:'unknown'})}
      });review.append(confirm,cancel)
    }catch{if(turn===serial && isCurrent())status.textContent='Checkout review unavailable.'}
  }
  const fetchForm=document.createElement('div');fetchForm.hidden=true
  const remoteName=document.createElement('input'),remoteBranch=document.createElement('input'),remoteFolder=document.createElement('button')
  remoteName.setAttribute('aria-label','Remote name');remoteName.placeholder='Remote name';remoteName.maxLength=64
  remoteBranch.setAttribute('aria-label','Remote branch');remoteBranch.placeholder='Remote branch';remoteBranch.maxLength=255
  remoteFolder.type='button';remoteFolder.textContent='Choose local remote folder';let remoteSelection=null
  for(const input of [remoteName,remoteBranch])input.addEventListener('input',()=>{if(!issued){serial++;pending=null;remoteSelection=null;review.replaceChildren()}})
  remoteFolder.addEventListener('click',async()=>{if(issued || !isCurrent())return;const turn=++serial;try{const selected=await selectFolder?.('remote-read');if(turn===serial && isCurrent() && selected){remoteSelection=selected;pending=null;review.replaceChildren();status.textContent='Local remote folder selected. Review the fetch.'}}catch{if(turn===serial && isCurrent())status.textContent='Remote folder unavailable.'}})
  fetchForm.append(remoteName,remoteBranch,remoteFolder);pane.insertBefore(fetchForm,status)
  const previewFetch=async()=>{
    const turn=++serial,body={version:1,chatID,rootRef,remoteName:remoteName.value,branch:remoteBranch.value,remoteRef:remoteSelection};review.replaceChildren();status.textContent='Reading remote branch…'
    try {
      const value=await post('fetch-preview',body);if(turn!==serial || !isCurrent())return
      if(value.state==='choose_remote'){status.textContent='Choose the configured local remote folder, then review again.';return}
      const r=value.review
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || typeof value.expectedDigest!=='string' || !r || r.remoteName!==body.remoteName || r.branch!==body.branch || typeof r.targetOID!=='string' || !(r.oldOID===null || typeof r.oldOID==='string') || !['local','https'].includes(r.transport) || r.remoteMayMove!==true)throw Error('Invalid fetch review')
      pending={kind:'fetch',requestID:value.requestID,expectedDigest:value.expectedDigest};status.textContent='Review the observed remote commit. Fetch transfers that exact commit; the remote may move afterward. Private authenticated remotes are unavailable.'
      const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText;summary.textContent=`Remote: ${r.remoteName}${r.transport==='https' && r.host ? ' · '+r.host : ' · local folder'}\nBranch: ${r.branch}\nObserved commit: ${r.targetOID}\nCurrent tracking commit: ${r.oldOID || 'absent'}`;review.append(summary)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Fetch reviewed commit';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=remoteName.disabled=remoteBranch.disabled=remoteFolder.disabled=true
        try{const result=await post('fetch-confirm',{...body,targetOID:r.targetOID,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)}catch{if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    }catch{if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Fetch review unavailable. Local remotes need Files access; HTTPS uses no credentials.'}}
  }
  const pushForm=document.createElement('div');pushForm.hidden=true
  const pushRemote=document.createElement('input'),pushBranch=document.createElement('input'),pushFolder=document.createElement('button')
  pushRemote.setAttribute('aria-label','Push remote name');pushRemote.placeholder='Remote name';pushRemote.maxLength=64
  pushBranch.setAttribute('aria-label','Push remote branch');pushBranch.placeholder='Remote branch';pushBranch.maxLength=255
  pushFolder.type='button';pushFolder.textContent='Choose local remote folder';let pushSelection=null
  for(const input of [pushRemote,pushBranch])input.addEventListener('input',()=>{if(!issued){serial++;pending=null;pushSelection=null;review.replaceChildren()}})
  pushFolder.addEventListener('click',async()=>{if(issued || !isCurrent())return;const turn=++serial;try{const selected=await selectFolder?.('remote-write');if(turn===serial && isCurrent() && selected){pushSelection=selected;pending=null;review.replaceChildren();status.textContent='Local remote folder selected. Review the push.'}}catch{if(turn===serial && isCurrent())status.textContent='Remote folder unavailable.'}})
  pushForm.append(pushRemote,pushBranch,pushFolder);pane.insertBefore(pushForm,status)
  const previewPush=async()=>{
    const turn=++serial,body={version:1,chatID,rootRef,remoteName:pushRemote.value,branch:pushBranch.value,remoteRef:pushSelection};review.replaceChildren();status.textContent='Reading remote branch…'
    try {
      const value=await post('push-preview',body);if(turn!==serial || !isCurrent())return
      if(value.state==='choose_remote'){status.textContent='Choose the configured local remote folder with write access, then review again.';return}
      const r=value.review
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || typeof value.expectedDigest!=='string' || !r || r.remoteName!==body.remoteName || r.branch!==body.branch || typeof r.targetOID!=='string' || !(r.observedOID===null || typeof r.observedOID==='string') || !['local','https'].includes(r.transport) || r.remoteMayMove!==true || r.force!==false)throw Error('Invalid push review')
      pending={kind:'push',requestID:value.requestID,expectedDigest:value.expectedDigest};status.textContent='Push this exact commit without force. The observed remote commit may change; the server checks fast-forward. Private authenticated remotes are unavailable.'
      const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText;summary.textContent=`Remote: ${r.remoteName}${r.transport==='https' && r.host ? ' · '+r.host : ' · local folder'}\nBranch: ${r.branch}\nCommit to push: ${r.targetOID}\nRemote commit observed at review: ${r.observedOID || 'absent'}`;review.append(summary)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Push reviewed commit';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=pushRemote.disabled=pushBranch.disabled=pushFolder.disabled=true
        try{const result=await post('push-confirm',{...body,targetOID:r.targetOID,observedOID:r.observedOID,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)}catch{if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    }catch{if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Push review unavailable. Local remotes need Files write access; HTTPS uses no credentials.'}}
  }
  const previewDiscard=async(fileRef)=>{
    const turn=++serial,body={version:1,chatID,rootRef,fileRef};review.replaceChildren();status.textContent='Preparing reviewed index version…'
    try{
      const value=await post('discard-preview',body);if(turn!==serial || !isCurrent())return
      if(value.state==='no_changes'){status.textContent='The file already matches the reviewed index bytes.';return}
      const r=value.review
      if(value.version!==1 || value.state!=='ready' || typeof value.requestID!=='string' || typeof value.expectedDigest!=='string' || !r || typeof r.name!=='string' || typeof r.indexOID!=='string' || typeof r.before!=='string' || typeof r.after!=='string' || !/^[0-9a-f]{64}$/.test(r.beforeSHA256) || !/^[0-9a-f]{64}$/.test(r.afterSHA256) || ['beforeCR','afterCR','beforeBOM','afterBOM'].some(k=>typeof r[k]!=='boolean') || r.indexObserved!==true || r.atomicIndex!==false)throw Error('Invalid discard review')
      pending={kind:'discard',requestID:value.requestID,expectedDigest:value.expectedDigest,fileRef};status.textContent='Restore the exact reviewed index bytes. History captures the current file; a later index change may leave unstaged differences.'
      const info=document.createElement('pre');info.style.cssText=detail.style.cssText;info.textContent=`File: ${r.name}\nIndex blob observed: ${r.indexOID}\nCurrent SHA-256: ${r.beforeSHA256} · CR: ${r.beforeCR?'yes':'no'} · BOM: ${r.beforeBOM?'yes':'no'}\nReviewed SHA-256: ${r.afterSHA256} · CR: ${r.afterCR?'yes':'no'} · BOM: ${r.afterBOM?'yes':'no'}`;review.append(info)
      for(const [label,text] of [['Current file',r.before],['Reviewed index version',r.after]]){const head=document.createElement('strong'),content=document.createElement('pre');head.textContent=label;content.style.cssText=detail.style.cssText;content.textContent=text;review.append(head,content)}
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Restore reviewed index version';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=true
        try{const result=await post('discard-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)}catch{if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    }catch{if(turn===serial && isCurrent()){pending=null;review.replaceChildren();status.textContent='Reviewed index version unavailable for this file.'}}
  }
  const choose = document.createElement('button'); choose.type = 'button'; choose.textContent = 'Choose file'
  choose.addEventListener('click', async () => {
    if (issued || !isCurrent()) return
    const operation=operationChoice.value, turn=++serial
    pending=null; review.replaceChildren(); choose.disabled=operationChoice.disabled=reviewButton.disabled=true
    try { const ref = operation==='cleanup' ? await selectFolder?.() : await selectFile?.(); if (ref && !issued && turn===serial && isCurrent() && operationChoice.value===operation) { if(operation==='cleanup')folderSelection=ref;else selection=ref; status.textContent=operation==='cleanup'?'Folder selected.':'File selected.' } }
    catch { if (turn===serial && isCurrent()) status.textContent = 'Selection unavailable.' }
    finally { if (!issued && isCurrent()) choose.disabled=operationChoice.disabled=reviewButton.disabled=false }
  })
  const worktreeForm=document.createElement('div');worktreeForm.hidden=true
  const worktreeTarget=document.createElement('input');worktreeTarget.placeholder='Exact commit ID';worktreeTarget.setAttribute('aria-label','Worktree commit ID');worktreeTarget.maxLength=64
  const worktreeName=document.createElement('input');worktreeName.placeholder='New folder name';worktreeName.setAttribute('aria-label','Worktree folder name');worktreeName.maxLength=255
  const worktreeFolder=document.createElement('button');worktreeFolder.type='button';worktreeFolder.textContent='Choose destination folder'
  const worktreeOwned=document.createElement('select');worktreeOwned.setAttribute('aria-label','Owned worktree operation')
  const worktreeSupport=document.createElement('p');worktreeSupport.textContent='Linked worktree create, Undo and Redo currently require Linux and available Files and History services.'
  let worktreeDestination='';worktreeForm.append(worktreeSupport,worktreeTarget,worktreeName,worktreeFolder,worktreeOwned);pane.insertBefore(worktreeForm,status)
  worktreeFolder.addEventListener('click',async()=>{if(issued || !isCurrent())return;const turn=++serial;try{const value=await selectFolder?.();if(value && turn===serial && isCurrent()){worktreeDestination=value;status.textContent='Destination selected.'}}catch{if(turn===serial && isCurrent())status.textContent='Destination unavailable.'}})
  const loadOwnedWorktrees=async(kind)=>{const turn=serial;worktreeOwned.replaceChildren();try{const value=await post('worktree-owned',{version:1,chatID,rootRef});if(turn!==serial || !isCurrent())return
    if(value.state!=='ready' || !Array.isArray(value.items))throw Error('unavailable');for(const item of value.items.filter(item=>item.action===kind)){const option=document.createElement('option');option.value=item.operationID;option.textContent=`${kind==='undo'?'Undo':'Redo'} · ${item.targetOID}`;worktreeOwned.append(option)}
    status.textContent=value.partial?'Owned operations: partial result.':'Select an owned operation.'
  }catch{if(turn===serial && isCurrent())status.textContent='Owned worktrees unavailable.'}}
  const previewWorktree=async(kind)=>{const turn=++serial;review.replaceChildren();status.textContent='Preparing worktree review…'
    const body={version:1,operation:kind,chatID,rootRef,...(kind==='create'?{destinationRef:worktreeDestination,name:worktreeName.value,targetOID:worktreeTarget.value.trim()}:{selectedOperationID:worktreeOwned.value})}
    try{const value=await post('worktree-preview',body);if(turn!==serial || !isCurrent())return
      if(value.state!=='ready' || !value.requestID || !value.expectedDigest || !value.review){status.textContent='Worktree review unavailable or unsupported.';return}
      pending={kind:'worktree',requestID:value.requestID,expectedDigest:value.expectedDigest};const summary=document.createElement('pre');summary.style.cssText=detail.style.cssText
      summary.textContent=`${kind==='create'?'Create detached worktree':kind==='undo'?'Undo unchanged owned worktree':'Redo owned worktree'}\nCommit: ${value.review.targetOID}\nFiles: ${value.review.files} · bytes: ${value.review.bytes}\n${kind==='create'?'The selected folder name must remain absent.':'Changed or occupied destinations are refused.'}`;review.append(summary)
      const confirm=document.createElement('button'),cancel=document.createElement('button');confirm.type=cancel.type='button';confirm.textContent='Confirm reviewed worktree';cancel.textContent='Cancel review'
      cancel.addEventListener('click',()=>{if(!issued){pending=null;review.replaceChildren();status.textContent='Review cancelled.'}})
      confirm.addEventListener('click',async()=>{if(issued || turn!==serial || !isCurrent() || pending?.requestID!==value.requestID)return;confirm.disabled=cancel.disabled=true;issued=true;choose.disabled=operationChoice.disabled=reviewButton.disabled=refresh.disabled=worktreeFolder.disabled=worktreeTarget.disabled=worktreeName.disabled=worktreeOwned.disabled=true
        try{const result=await post('worktree-confirm',{...body,requestID:pending.requestID,expectedDigest:pending.expectedDigest});if(!current())return;await onPolicyTransition?.(result,pending);if(current())renderStatus(result)}catch{if(current())renderStatus({state:'unknown'})}
      },{once:true});review.append(confirm,cancel)
    }catch{if(turn===serial && isCurrent())status.textContent='Worktree review unavailable.'}}
  const operationChoice = document.createElement('select'); operationChoice.setAttribute('aria-label', 'Git operation')
  for (const [value,label] of [['stage','Stage file'],['unstage','Unstage file'],['stage_hunks','Stage selected changes'],['unstage_hunks','Unstage selected changes'],['commit','Commit staged tree'],['branch','Create branch'],['switch','Switch branch'],['pull','Fast-forward to tracking branch'],['fetch','Fetch remote branch'],['push','Push current commit'],['discard','Restore reviewed index version'],['worktree-create','Create linked worktree'],['worktree-undo','Undo owned worktree'],['worktree-redo','Redo owned worktree'],['cleanup','Remove linked worktree']]) { const option = document.createElement('option'); option.value = value; option.textContent = label; operationChoice.append(option) }
  operationChoice.addEventListener('change', () => { if (!issued) { serial++; pending = null; review.replaceChildren(); commitForm.hidden=operationChoice.value!=='commit'; branchForm.hidden=operationChoice.value!=='branch'; fetchForm.hidden=operationChoice.value!=='fetch'; pushForm.hidden=operationChoice.value!=='push'; checkoutForm.hidden=!['switch','pull'].includes(operationChoice.value); worktreeForm.hidden=!operationChoice.value.startsWith('worktree-'); worktreeTarget.hidden=worktreeName.hidden=worktreeFolder.hidden=operationChoice.value!=='worktree-create';worktreeOwned.hidden=!['worktree-undo','worktree-redo'].includes(operationChoice.value);if(!worktreeOwned.hidden)void loadOwnedWorktrees(operationChoice.value==='worktree-undo'?'undo':'redo'); choose.hidden=['commit','branch','switch','pull','fetch','push','worktree-create','worktree-undo','worktree-redo'].includes(operationChoice.value); choose.textContent=operationChoice.value==='cleanup'?'Choose folder':'Choose file' } })
  const reviewButton = document.createElement('button'); reviewButton.type = 'button'; reviewButton.textContent = 'Review'
  reviewButton.addEventListener('click', () => { if (!pending) { const operation = operationChoice.value || 'stage'; if(operation.startsWith('worktree-')) void previewWorktree(operation.slice(9)); else if(operation==='commit') void previewCommit(); else if(operation==='branch') void previewBranch(); else if(operation==='switch' || operation==='pull') void previewCheckout(operation); else if(operation==='fetch') void previewFetch(); else if(operation==='push') void previewPush(); else if(operation==='discard') {if(selection) void previewDiscard(selection); else status.textContent='Choose a file.'} else if(operation==='cleanup') {if(folderSelection) void previewCleanup(folderSelection); else status.textContent='Choose a folder.'} else if(selection) void preview(operation, operation.endsWith('_hunks') ? [] : null); else status.textContent='Choose a file.' } })
  pane.insertBefore(choose, status); pane.insertBefore(operationChoice, status); pane.insertBefore(reviewButton, status)
  const renderInventory = (result) => {
    if (result.state !== 'ready') { status.textContent = 'Git workbench unavailable or unsupported.'; return }
    snapshot = result.snapshot
    status.textContent = result.partial ? 'Partial inventory of working and staged changes.' : 'Working and staged changes.'
    const rows = [...(activeFile ? [{path:activeFile, index:'', worktree:'', active:true}] : []), ...result.items.filter(item => item.path !== activeFile)]
    for (const item of rows) {
      const row = document.createElement('div'); const label = document.createElement('span')
      label.textContent = item.active ? `Active file: ${item.path} ` : `${item.index}${item.worktree} ${item.path} `; row.append(label)
      for (const [operation, name] of [['worktree','Working'],['staged','Staged'],['headDiff','HEAD']]) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = name
        button.addEventListener('click', async () => {
          if (issued || !isCurrent()) return
          const turn = ++serial; detail.textContent = 'Reading diff…'
          try { const value = await read({operation, path:item.path, expected:snapshot}); if (turn === serial && isCurrent()) detail.textContent = value.state === 'ready' ? (value.diff || '(No textual diff)') : value.state === 'stale' ? 'Changed since refresh. Refresh to continue.' : 'Diff unavailable or unsupported.' }
          catch { if (turn === serial && isCurrent()) detail.textContent = 'Diff unavailable.' }
        }); row.append(button)
      }
      list.append(row)
    }
  }
  refresh.addEventListener('click', async () => {
    if (issued || !isCurrent()) return
    const own = ++serial; snapshot = null; list.replaceChildren(); detail.textContent = ''; status.textContent = 'Reading…'
    try {
      const result = await read({operation:'status'})
      if (own === serial && isCurrent()) renderInventory(result)
    } catch { if (own === serial && isCurrent()) status.textContent = 'Git workbench unavailable.' }
  })
  if (initialStatus) renderInventory(initialStatus)
  return {
    element: pane,
    enterStatusOnly() {
      if (statusOnly) return
      statusOnly = true; serial++; selection = ''; folderSelection = ''; snapshot = null
      list.replaceChildren(); detail.textContent = ''
      choose.disabled = operationChoice.disabled = reviewButton.disabled = refresh.disabled = true
      for (const button of repositoryReads.children) button.disabled = true
      if (!issued) {
        pending = null
        review.replaceChildren()
        status.textContent = 'File access changed. Reopen the Git workbench to continue.'
      }
    },
    dispose() { serial++; controller.abort(); selection = ''; folderSelection = ''; pending = null; pane.remove() },
  }
}
