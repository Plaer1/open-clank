import sessionModule from './sessions.js';
import { getWorkspace } from './workspace.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { captureContinuityScope, continuityScopeCurrent } from './chatContinuityPanel.js';
import { openAgentRunInspector } from './agentRunInspector.js';
import { renderActorAccounting } from './chatRenderer.js';
import { folderChangeLabel, validFolderChanges } from './historyFolderModel.js';
import { mountTaskWorkload } from './taskWorkload.js';
import * as attachments from './fileHandler.js';

const button = document.getElementById('chat-workspace-button');
const panel = document.getElementById('chat-workspace-panel');
const CHAT_HISTORY = '#chat-history';
const knownStatuses = new Map([
  ['running','Running'], ['waiting','Waiting'], ['paused','Waiting'],
  ['failed','Failed'], ['completed','Done'], ['done','Done']
]);
const sameRevision = (a,b) => !!a?.kind && !!a?.value && a.kind === b?.kind && a.value === b?.value;

async function read(url, options = {}) {
  const response = await fetch(url, { credentials:'same-origin', cache:'no-store', ...options });
  if (!response.ok) throw new Error('Request unavailable');
  return response.json();
}

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function action(text, run, className) {
  const el = node('button', text, className);
  el.type = 'button';
  el.addEventListener('click', run);
  return el;
}

function section(name) {
  const root = node('section');
  const heading = node('h3', name);
  const tools = node('div', null, 'chat-workspace-tools');
  const head = node('header');
  head.append(heading, tools);
  const body = node('div', null, 'chat-workspace-section-body');
  root.append(head, body);
  return {root,heading,tools,body};
}

function capture() {
  const scope = captureContinuityScope();
  const session = sessionModule.getSessions().find(row => row.id === scope.chat);
  if (!scope.account || sessionModule.isCurrentSessionIncognito()) return null;
  return {...scope, hasFolder:Boolean(scope.workspace && session?.workspace_id === scope.workspace),
    navigation:sessionModule.getSessionNavigationToken()};
}

function scopeCurrent(scope) {
  const now = capture();
  return !!now && continuityScopeCurrent(scope) && now.navigation === scope.navigation && now.hasFolder === scope.hasFolder;
}

function actorPage(value, chat) {
  const scope = value?.scope, page = value?.sources?.native_actor;
  if (scope?.hostChatId !== chat || typeof scope.rootSessionId !== 'string' || !scope.rootSessionId ||
      !Number.isSafeInteger(scope.mapRevision) || !['available','partial'].includes(page?.availability) ||
      !Array.isArray(page.items) || page.items.length > 20) throw new Error('Workers unavailable');
  const actors = new Map();
  let partial = page.availability === 'partial' || !!page.nextCursor;
  for (const row of page.items) {
    const ref = row?.sourceRef;
    if ((row?.hostChatId != null && row.hostChatId !== chat) || row?.displayCategory !== 'execution' ||
        ref?.kind !== 'native_actor' || ref.sessionId !== scope.rootSessionId ||
        typeof ref.nativeId !== 'string' || !ref.nativeId ||
        !Number.isSafeInteger(ref.executionRevision) || ref.executionRevision < 0) {
      partial = true;
      continue;
    }
    if (ref.nativeId === 'main') continue;
    const prior = actors.get(ref.nativeId);
    if (!prior || prior.sourceRef.executionRevision < ref.executionRevision) actors.set(ref.nativeId, row);
  }
  const rows = [...actors.values()];
  return { rows, scope, partial };
}

function sourceCards() {
  const cards = new Map();
  for (const card of document.querySelectorAll(
    CHAT_HISTORY + ' .attach-cards > :is(.attach-image-preview,.attach-card)[data-file-id]'
  )) {
    if (!card.isConnected || card.closest('[hidden]')) continue;
    const name = card.querySelector('.attach-image-name, .attach-card-name')?.textContent || card.dataset.name || 'Chat attachment';
    cards.set(card.dataset.fileId, {name,card});
  }
  return [...cards.values()];
}

function mount() {
  let active = null;
  let policyEpoch = 0;
  const pickedFiles = new WeakMap();
  const chat = button.closest('#chat-container');
  let wide = false;
  let floating = false;
  let preferenceKey = '';
  let pinned = true;

  function toggle() {
    if (wide) {
      pinned = !pinned;
      if (capture()?.chat) {
        try {
          if (pinned) localStorage.removeItem(preferenceKey);
          else localStorage.setItem(preferenceKey, 'closed');
        } catch {}
      }
    } else floating = !floating;
    sync();
    if (!active) button.focus();
    else if (!wide) panel.focus();
  }

  function close(returnFocus = true) {
    const state = active;
    if (!state) return;
    active = null;
    delete chat.dataset.statusDocked;
    state.controller.abort();
    state.picker?.destroy();
    state.inspector?.dispose();
    state.observer?.disconnect();
    panel.close();
    panel.replaceChildren();
    button.setAttribute('aria-expanded','false');
    if (returnFocus && !button.hidden) button.focus();
  }

  function dismissOverlay() {
    if (wide) return;
    floating = false;
    close(false);
  }

  function sync() {
    const scope = capture();
    const key = scope ? 'openclank.chat-status.' + JSON.stringify([scope.account,scope.chat]) : '';
    if (active && !scopeCurrent(active.scope)) { close(false); floating = false; }
    if (key !== preferenceKey) {
      preferenceKey = key;
      pinned = true;
      floating = false;
      try { if (scope?.chat) pinned = localStorage.getItem(key) !== 'closed'; } catch {}
    }
    const roomy = chat.getBoundingClientRect().width >= 900;
    if (roomy !== wide) floating = false;
    wide = roomy;
    button.hidden = !scope;
    if (!scope || !(wide ? pinned : floating)) { close(false); return; }
    chat.toggleAttribute('data-status-docked', wide);
    if (!active) open(scope);
  }

  function open(scope = capture()) {
    if (!scope || active) return;
    const state = active = { scope, controller:new AbortController(), files:null, picker:null, inspector:null, allSources:false };
    const current = () => active === state && scopeCurrent(scope) && panel.open;
    const signal = state.controller.signal;
    const openingPolicy = policyEpoch;
    const workspaceCurrent = () => scopeCurrent(scope) && policyEpoch === openingPolicy;
    const files = section('History');
    const tasks = section('Task list');
    const artifacts = section('Artifacts');
    const workers = section('Subagents');
    const sources = section('Sources');
    const detail = node('div', null, 'chat-workspace-detail');
    const status = node('p', null, 'chat-workspace-status');
    status.setAttribute('role','status');
    const panelName = scope.hasFolder ? getWorkspace().split(/[\\/]/).filter(Boolean).at(-1) || 'Workspace' : 'Chat status';
    const title = scope.hasFolder ? action(panelName, async () => {
      if (!current() || !state.files) return;
      try {
        const {revealResource} = await import('./files.js');
        if (!current()) return;
        if (!await revealResource(state.files.resource.ref, {isCurrent:current})) throw new Error();
        if (current()) dismissOverlay();
      } catch { if (current()) status.textContent = 'The workspace folder could not be opened.'; }
    }, 'chat-workspace-title') : node('strong', panelName, 'chat-workspace-title');
    if (scope.hasFolder) {
      title.title = 'Open workspace in Files';
      title.disabled = true;
    }
    files.body.textContent = 'Reading recorded changes…';
    workers.body.textContent = 'Reading worker observations…';
    const nav = node('div', null, 'chat-workspace-nav');
    nav.append(title);
    panel.append(nav, tasks.root, artifacts.root, workers.root, files.root, sources.root, status, detail);
    detail.hidden = true;
    const priorFocus = document.activeElement;
    panel.show();
    if (wide && priorFocus instanceof HTMLElement && panel.contains(document.activeElement)) priorFocus.focus({preventScroll:true});
    button.setAttribute('aria-expanded','true');

    const openWork = action('Chat work', () => {
      if (!current()) return;
      state.inspector?.dispose();
      detail.replaceChildren();
      detail.hidden = false;
      state.inspector = mountTaskWorkload(detail);
      detail.scrollIntoView({block:'nearest'});
    });
    if (scope.chat) workers.tools.append(openWork);
    const viewAll = action('View all', () => { state.allSources = !state.allSources; paintSources(); });
    const context = action('Model context', async () => {
      try {
        const {mountChatContextInspector} = await import('./chatContextInspector.js');
        if (!current()) return;
        state.inspector?.dispose();
        detail.hidden = false;
        detail.replaceChildren();
        state.inspector = mountChatContextInspector(detail, () => current() ? scope.chat : '');
        detail.scrollIntoView({block:'nearest'});
      } catch { if (current()) status.textContent = 'Model context is unavailable.'; }
    });
    const add = action('+', () => {
      void pickSource().catch(() => { if (current()) status.textContent = 'The Files picker is unavailable.'; });
    }, 'chat-workspace-add');
    add.setAttribute('aria-label', 'Add workspace file to chat draft');
    add.disabled = true;
    sources.tools.append(viewAll);
    if (scope.hasFolder) sources.tools.prepend(add);
    if (scope.chat) sources.tools.append(context);

    function paintSources() {
      if (!current()) return;
      const shown = [];
      for (const file of attachments.getPendingRaw()) {
        const line = node('div', null, 'chat-workspace-row');
        line.append(node('span', file.name || 'Attachment'), node('small', 'Draft'));
        shown.push(line);
      }
      for (const {name,card} of sourceCards()) {
        const label = action(name, () => { if (current() && card.isConnected) card.click(); }, 'chat-workspace-row chat-workspace-link');
        label.title = name;
        shown.push(label);
      }
      sources.heading.textContent = `Sources · ${shown.length} shown in chat`;
      viewAll.hidden = shown.length <= 4;
      viewAll.textContent = state.allSources ? 'Show less' : 'View all';
      sources.body.replaceChildren(...shown.slice(0,state.allSources ? undefined : 4));
      if (!shown.length) sources.body.append(node('p','No sources in the draft or loaded messages.'));
    }

    async function pickSource() {
      if (!current() || !state.files || state.picker) return;
      const originalDraft = window.chatModule?.captureComposerDraftState?.();
      if (!originalDraft || originalDraft.sid !== scope.chat) return;
      const draftCurrent = () => {
        const draft = window.chatModule?.captureComposerDraftState?.();
        return current() && draft?.sid === originalDraft.sid && draft.epoch === originalDraft.epoch &&
          draft.version === originalDraft.version && draft.pending.length === originalDraft.pending.length &&
          draft.pending.every((file, index) => file === originalDraft.pending[index]);
      };
      const {createResourcePicker} = await import('./copal/resourcePicker.js');
      if (!draftCurrent() || state.picker) return;
      state.picker = createResourcePicker({client:filesFacadeClient,purpose:'snapshot-file',
        accountScope:scope.account,workspaceScope:scope.workspace,generation:state.files.generation,
        initialDirectory:state.files.resource,rootLabel:'Add workspace source',signal,isOriginCurrent:current,
        onClose:() => { state.picker = null; },
        onSelect:async selection => {
          if (!draftCurrent()) throw new Error('The chat draft changed. Reopen the picker.');
          if (!selection.revision) throw new Error('The file revision is unavailable.');
          const key = JSON.stringify([scope.workspace,selection.resourceId,selection.revision]);
          if (originalDraft.pending.some(file => pickedFiles.get(file) === key)) throw new Error('This revision is already attached.');
          const [root,revealed] = await Promise.all([
            filesFacadeClient.workspaceResource(scope.workspace,'',{signal}),
            filesFacadeClient.reveal(selection.ref,{signal})
          ]);
          if (!draftCurrent()) throw new Error('The chat draft changed. Reopen the picker.');
          if (root.workspace?.revision !== state.files.workspace.revision || root.generation !== state.files.generation ||
              root.resource?.id !== state.files.resource.id || revealed.resource?.id !== selection.resourceId ||
              !sameRevision(revealed.resource.revision,selection.revision) ||
              ![revealed.parent,...(revealed.ancestors || [])].some(dir => dir?.id === root.resource.id))
            throw new Error('Choose an unchanged file in the current workspace.');
          const response = await fetch(filesFacadeClient.contentUrl(selection.ref,{expectedRevision:selection.revision}),
            {credentials:'same-origin',cache:'no-store',signal});
          if (!response.ok) throw new Error('The selected file changed or is unavailable.');
          const blob = await response.blob();
          if (!draftCurrent()) throw new Error('The chat draft changed. Reopen the picker.');
          const file = new File([blob], selection.name, {type:blob.type});
          await attachments.addFiles([file],{skipCrop:true});
          if (!attachments.getPendingRaw().includes(file)) throw new Error('The draft attachment limit was reached.');
          pickedFiles.set(file,key);
          return true;
        }
      });
      const picker = state.picker;
      if (!await picker.open() && current()) {
        picker.destroy();
        status.textContent = 'The Files picker is unavailable.';
      }
    }

    const history = document.querySelector(CHAT_HISTORY);
    const draftStrip = document.getElementById('attach-strip');
    const planDock = document.getElementById('mimo-plan-dock');
    let nativeTasks = [], taskPartial = false, taskUnavailable = false;
    function paintTasks() {
      const rows = nativeTasks.map(row => [row.title, row.nativeStatus]);
      if (planDock && !planDock.hidden && planDock.dataset.chatId === scope.chat) {
        for (const item of planDock.querySelectorAll('[data-plan-todos] li')) {
          if (!rows.some(([title]) => title === item.textContent)) rows.push([item.textContent, item.dataset.status]);
        }
      }
      const done = rows.filter(([,status]) => ['done','completed'].includes(status)).length;
      tasks.heading.textContent = rows.length ? `Task list · ${done}/${rows.length}${taskPartial ? '+' : ''}` : 'Task list';
      tasks.body.replaceChildren(...rows.map(([title,status]) => {
        const line = node('div', null, 'chat-workspace-row');
        line.append(node('span', title), node('small', (status || 'pending').replaceAll('_',' ')));
        return line;
      }));
      if (!rows.length) tasks.body.append(node('p',taskUnavailable ? 'Task observations unavailable.' : 'No tasks yet.'));
    }
    const outputs = '[data-chat-artifact], .generated-image, .agent-tool-diff .diff-file';
    function paintArtifacts() {
      const rows = new Map();
      for (const item of history?.querySelectorAll(outputs) || []) {
        if (item.hidden || item.closest('.agent-thread-node.error')) continue;
        const diff = item.closest('.agent-thread-node')?.querySelector('.diff-file')?.textContent;
        const name = diff || item.dataset.chatArtifact || item.alt || item.textContent;
        const key = item.closest('.generated-image-wrap')?.dataset.imageKey || (diff ? 'file:' + diff : item);
        if (item.matches('.diff-file') && rows.has(key)) continue;
        rows.set(key, action(name, () => {
          if (!current() || !item.isConnected) return;
          if (item.matches('.diff-file')) {
            item.closest('.agent-thread-node')?.classList.add('open');
            item.closest('details').open = true;
            item.scrollIntoView({block:'center'});
          } else item.click();
        }, 'chat-workspace-row chat-workspace-link'));
      }
      artifacts.heading.textContent = rows.size ? `Artifacts · ${rows.size}` : 'Artifacts';
      artifacts.body.replaceChildren(...rows.values());
      if (!rows.size) artifacts.body.append(node('p','No outputs in the loaded messages.'));
    }
    paintArtifacts();
    paintTasks();
    state.observer = new MutationObserver(records => {
      if (!current()) return;
      if (records.some(record => planDock?.contains(record.target))) paintTasks();
      if (records.some(record => [...record.addedNodes,...record.removedNodes].some(el =>
        el.nodeType === 1 && (el.matches(outputs) || el.querySelector(outputs))))) paintArtifacts();
      const match = element => element.nodeType === Node.ELEMENT_NODE &&
        (element.matches('.attach-cards') || element.querySelector('.attach-cards'));
      if (records.some(record => draftStrip?.contains(record.target) || record.target.closest?.('.attach-cards') ||
        [...record.addedNodes, ...record.removedNodes].some(match))) paintSources();
    });
    for (const target of [history,draftStrip])
      if (target) state.observer.observe(target,{childList:true,subtree:true});
    if (planDock) state.observer.observe(planDock,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden','data-chat-id']});
    paintSources();

    function showWorkers(page) {
      const counts = new Map();
      const rows = [];
      for (const row of page.rows) {
        const ref = row.sourceRef;
        const label = knownStatuses.get(row.nativeStatus) || row.nativeStatus || 'Unknown';
        counts.set(label, (counts.get(label) || 0) + 1);
        const worker = action(`${row.title || ref.nativeId} · ${label}`, () => inspect(row,worker), 'chat-workspace-row chat-workspace-link');
        worker.title = `${row.title || ref.nativeId} · ${ref.sessionId}`;
        rows.push(worker);
      }
      const coverage = [...counts].map(([label,n]) => `${label} ${n}`).join(' · ');
      workers.body.replaceChildren(node('p', `Root session${page.partial ? ' · partial' : ''}${coverage ? ' · ' + coverage : ' · no workers observed'}`), ...rows);
      function inspect(row, opener) {
        if (!current()) return;
        state.inspector?.dispose();
        detail.hidden = false;
        detail.replaceChildren();
        state.inspector = openAgentRunInspector({
          container:detail, scope:{chat:scope.chat,mapRevision:page.scope.mapRevision}, row, current, opener,
          relations:{rootSessionId:page.scope.rootSessionId,actors:page.rows.map(row=>({row,mapRevision:page.scope.mapRevision,peer:false}))},
          renderAccounting:renderActorAccounting,
          sendFollowUp:async args=>(await import('./chat.js')).submitActorFollowUp(args),
          onSelectActor:entry=>inspect(entry.row,opener),
          onReturnToChat:()=>{state.inspector?.dispose();state.inspector=null;detail.hidden=true;dismissOverlay();document.getElementById('message')?.focus();},
          onRefresh:()=>{close(false);sync();},
          onClose:()=>{detail.hidden=true;state.inspector=null;}
        });
      }
    }

    async function loadFiles() {
      try {
        const result = await filesFacadeClient.workspaceResource(scope.workspace,'',{signal});
        if (!current()) return;
        if (result.workspace?.id !== scope.workspace || result.resource?.provider !== 'host' ||
            result.resource?.kind !== 'folder' || !result.resource.ref || !result.resource.id) throw new Error();
        state.files = result;
        const folderRef = result.resource.ref;
        const name = result.resource.name || result.workspace.name || panelName;
        title.textContent = result.workspace.name || panelName;
        title.disabled = false;
        add.disabled = false;
        files.tools.append(action('Folder History…', async () => {
          const { openFolderHistory } = await import('./historyView.js');
          if (current() && workspaceCurrent() && openFolderHistory({ folderRef, name, isContextCurrent: workspaceCurrent }, button)) dismissOverlay();
        }));
        nav.append(action('Git workbench…', async event => {
          if (!current()) return;
          const opener = event.currentTarget;
          opener.disabled = true;
          try {
            const { openGitWorkbenchWindow } = await import('./gitWorkbench.js');
            if (!current() || !workspaceCurrent()) return;
            const opened = await openGitWorkbenchWindow({
              rootRef: folderRef, name, chatID: scope.chat, account: scope.account,
              generation: result.generation, signal, trigger: button,
              isCurrent: workspaceCurrent, isStatusCurrent: () => scopeCurrent(scope),
              setStatus: message => { if (current()) status.textContent = message; },
            });
            if (opened && current()) dismissOverlay();
          } finally {
            if (current()) opener.disabled = false;
          }
        }));

        const query = new URLSearchParams({ limit:'10', cursor:'0', folder_ref:folderRef });
        const history = await read(`/api/history/resources/folder-changes?${query}`, { signal }).catch(() => null);
        if (!current() || !workspaceCurrent()) return;
        if (!validFolderChanges(history)) {
          files.body.textContent = 'Recorded changes are unavailable.';
          return;
        }
        const rows = history.items.map(item => action(folderChangeLabel(item), async () => {
          const { openResourceHistory } = await import('./historyView.js');
          if (current() && workspaceCurrent() && openResourceHistory({
            resourceId: item.resource_id, name: item.name, provider: 'host', isContextCurrent: workspaceCurrent,
          }, button)) dismissOverlay();
        }, 'chat-workspace-row chat-workspace-link'));
        files.body.replaceChildren(...(rows.length ? rows : [node('p','No recorded changes in this folder.')]));
      } catch { if (current()) files.body.textContent = 'Workspace Files are unavailable.'; }
    }

    if (scope.hasFolder) void loadFiles();
    else files.root.remove();
    if (!scope.chat) {
      workers.body.textContent = 'No subagents yet.';
      return;
    }
    async function refreshWorkers() {
      if (state.refreshing) { state.refreshAgain = true; return; }
      state.refreshing = true;
      const taskRead = read(`/api/orchestration/${encodeURIComponent(scope.chat)}/observations/native_task?limit=50`,{signal}).then(result => {
        if (!current()) return;
        const page = result.sources?.native_task;
        if (result.scope?.hostChatId !== scope.chat || !Array.isArray(page?.items) || !['available','partial'].includes(page.availability)) throw new Error();
        nativeTasks = page.items.filter(row => row.hostChatId === scope.chat && row.sourceRef?.kind === 'native_task');
        taskPartial = !!page.nextCursor || page.availability === 'partial';
        taskUnavailable = false;
        paintTasks();
      }).catch(() => { if (current()) { taskUnavailable = true; paintTasks(); } });
      try { await Promise.all([taskRead, read(`/api/orchestration/${encodeURIComponent(scope.chat)}/observations/native_actor?limit=20`,{signal})
      .then(result => { if (current()) showWorkers(actorPage(result,scope.chat)); })
      .catch(() => { if (current()) workers.body.textContent='Worker observations are unavailable.'; })]);
      } finally {
        state.refreshing = false;
        if (state.refreshAgain && current()) { state.refreshAgain = false; void refreshWorkers(); }
      }
    }
    window.addEventListener('odysseus:chat-busy-change', refreshWorkers, {signal});
    void refreshWorkers();
  }

  button.addEventListener('click', toggle);
  new ResizeObserver(sync).observe(chat, {box:'border-box'});
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); toggle(); } });
  document.addEventListener('pointerdown', event => {
    if (!wide && active && !active.picker && !event.composedPath().some(node => node === panel || node === button)) { floating = false; close(false); }
  });
  for (const event of ['odysseus:session-selected','openclank:chat-status-scope','openclank:auth-context-changed','openclank:auth-user-ready','openclank:file-policy-changed']) {
    document.addEventListener(event,sync);
    window.addEventListener(event,sync);
  }
  window.addEventListener('workspace-change',sync);
  document.addEventListener('openclank:file-policy-changed', () => { policyEpoch += 1; close(false); sync(); });
  sync();
}

if (button && panel) mount();
